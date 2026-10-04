"""Bounded numerical/dependency witness for supported JAX versions, not a benchmark."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import time


def run(expected_version, backend):
    import numpy as np
    import jax
    import jax.numpy as jnp
    jax.config.update('jax_enable_x64', True)
    import blackjax
    import optax
    import nufftax

    assert jax.__version__ == expected_version
    assert importlib.metadata.version('jaxlib') == expected_version
    devices = jax.devices()
    expected_platform = 'gpu' if backend == 'cuda' else 'cpu'
    assert all(d.platform == expected_platform for d in devices), devices
    rng = np.random.default_rng(810)
    design = rng.normal(size=(40, 12))
    data = rng.normal(size=40)
    penalty = np.diag(np.linspace(0.5, 2, 12))
    gram, rhs = design.T @ design, design.T @ data

    def likelihood(theta):
        matrix = jnp.asarray(gram) + jnp.exp(theta) * jnp.asarray(penalty)
        chol = jnp.linalg.cholesky(matrix)
        solution = jax.scipy.linalg.cho_solve((chol, True), jnp.asarray(rhs))
        return -0.5 * (jnp.asarray(data @ data) - rhs @ solution + 2 * jnp.log(jnp.diag(chol)).sum())

    def reference(theta):
        matrix = gram + np.exp(theta) * penalty
        return -0.5 * (data @ data - rhs @ np.linalg.solve(matrix, rhs) + np.linalg.slogdet(matrix)[1])

    theta = np.array([-1.0, 0.1, 1.2])
    compiled = jax.jit(jax.vmap(jax.value_and_grad(likelihood)))
    start = time.monotonic()
    values, gradients = jax.block_until_ready(compiled(jnp.asarray(theta)))
    compile_execute_s = time.monotonic() - start
    start = time.monotonic()
    for _ in range(10):
        jax.block_until_ready(compiled(jnp.asarray(theta)))
    steady_s = (time.monotonic() - start) / 10
    reference_values = np.array([reference(x) for x in theta])
    reference_grad = np.array([(reference(x + 1e-5) - reference(x - 1e-5)) / 2e-5 for x in theta])
    np.testing.assert_allclose(values, reference_values, rtol=1e-10, atol=1e-10)
    np.testing.assert_allclose(gradients, reference_grad, rtol=1e-6, atol=1e-7)

    # Independent direct DFT oracle for the type-2 NUFFT used by interferometry.
    x, y = rng.uniform(-np.pi, np.pi, size=(2, 7))
    modes = rng.normal(size=(8, 8)) + 1j * rng.normal(size=(8, 8))
    frequencies = np.arange(-4, 4)
    dft = np.einsum('kj,mj,mk->m', modes, np.exp(-1j * x[:, None] * frequencies), np.exp(-1j * y[:, None] * frequencies))
    nufft = nufftax.nufft2d2(jnp.asarray(x), jnp.asarray(y), jnp.asarray(modes), eps=1e-10)
    np.testing.assert_allclose(nufft, dft, rtol=1e-7, atol=1e-7)

    # A real optimizer and a short NUTS chain catch dependency API breakage.
    # This checks execution/finite outputs, not posterior convergence.
    target = jnp.array([1.0, -2.0])
    objective = lambda p: jnp.sum((p - target) ** 2)
    optimizer = optax.adam(0.1)
    position = jnp.zeros(2)
    state = optimizer.init(position)
    @jax.jit
    def step(position, state):
        updates, state = optimizer.update(jax.grad(objective)(position), state, position)
        return optax.apply_updates(position, updates), state
    for _ in range(250):
        position, state = step(position, state)
    np.testing.assert_allclose(position, target, atol=1e-4, rtol=0)
    sampler = blackjax.nuts(lambda p: -0.5 * jnp.sum(p ** 2), step_size=0.2, inverse_mass_matrix=jnp.ones(2))
    state = sampler.init(jnp.zeros(2))
    sampler_step = jax.jit(sampler.step)
    for key in jax.random.split(jax.random.key(24), 16):
        state, info = sampler_step(key, state)
        assert np.all(np.isfinite(state.position))
        assert not bool(info.is_divergent)

    return {
        'diagnostic_only': True, 'python': platform.python_version(),
        'backend': backend, 'devices': [str(d) for d in devices],
        'packages': {p: importlib.metadata.version(p) for p in ('jax', 'jaxlib', 'numpy', 'scipy', 'blackjax', 'optax', 'nufftax')},
        'xla_flags': os.environ.get('XLA_FLAGS'),
        'values': np.asarray(values).tolist(), 'gradients': np.asarray(gradients).tolist(),
        'max_value_error': float(np.max(np.abs(values - reference_values))),
        'max_gradient_error': float(np.max(np.abs(gradients - reference_grad))),
        'max_nufft_error': float(np.max(np.abs(nufft - dft))),
        'compile_execute_s': compile_execute_s, 'steady_s': steady_s,
        'optimizer_position': np.asarray(position).tolist(), 'nuts_steps': 16,
        'status': 'pass',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--backend', choices=['cpu', 'cuda'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Set before JAX import. A missing GPU must fail instead of silently falling back.
    os.environ['JAX_PLATFORMS'] = args.backend
    os.environ['JAX_PLATFORM_NAME'] = 'gpu' if args.backend == 'cuda' else 'cpu'
    os.environ.setdefault('XLA_PYTHON_CLIENT_PREALLOCATE', 'false')
    result = run(args.version, args.backend)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
