# Creates the dedicated Julia environment used by the "Sienna" baseline
# (PowerSystems.jl + PowerSimulations.jl).
#
#   julia setup_sienna_env.jl
#
# The environment is created at ~/.julia/environments/sienna_bench and is
# stacked onto LOAD_PATH at runtime by the benchmark's Sienna adapter, so
# ESFEX's own Julia project is never modified.
#
# JuMP / MathOptInterface / HiGHS / Ipopt / PowerModels are pinned to the
# exact versions shipped in ESFEX's embedded Julia project: both stacks
# load in one Julia session, and Julia can only load one version of a
# package per session.  If you upgrade ESFEX, re-check these pins against
# `src/esfex/julia/Manifest.toml`.

using Pkg

env = joinpath(homedir(), ".julia", "environments", "sienna_bench")
Pkg.activate(env)

Pkg.add([
    PackageSpec(name = "PowerSystems"),
    PackageSpec(name = "PowerSimulations"),
    PackageSpec(name = "PowerModels", version = "0.21.6"),
    PackageSpec(name = "JuMP", version = "1.30.1"),
    PackageSpec(name = "MathOptInterface", version = "1.51.1"),
    PackageSpec(name = "HiGHS", version = "1.23.0"),
    PackageSpec(name = "Ipopt", version = "1.14.3"),
    # Must match the OpenSSL_jll version in juliacall's own environment
    # (PythonCall pins it to Python's OpenSSL; check with
    #  `grep -A6 OpenSSL_jll <juliacall julia_env>/Manifest.toml`).
    # A mismatch breaks HDF5_jll (PowerSystems dep) precompilation when
    # Sienna loads inside the same session as ESFEX.
    PackageSpec(name = "OpenSSL_jll", version = "3.0.16"),
])
Pkg.pin([
    PackageSpec(name = "PowerModels"),
    PackageSpec(name = "JuMP"),
    PackageSpec(name = "MathOptInterface"),
    PackageSpec(name = "OpenSSL_jll"),
])
Pkg.precompile()

println("sienna_bench environment ready at ", env)
