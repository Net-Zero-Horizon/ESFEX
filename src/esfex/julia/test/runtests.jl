# Native Julia test suite for the ESFEX optimization core.
#
# Run with:  julia --project=src/esfex/julia -e 'using Pkg; Pkg.test(coverage=true)'
# (or `using ESFEX` after activating + instantiating the project).
#
# These are unit tests for the pure / self-contained pieces of the Julia core.
# Integration tests that build and solve full optimization models live in the
# Python `julia`-marked suite (tests/test_power_system_parity.py, etc.).

using Test
using ESFEX

@testset "ESFEX" begin

    @testset "version" begin
        @test ESFEX.version() == "0.1.0"
        @test ESFEX.version() isa String
    end

    @testset "parse_acopf_formulation" begin
        @test parse_acopf_formulation("acopf_soc") isa ESFEX.SOCFormulation
        @test parse_acopf_formulation("acopf_qc") isa ESFEX.QCFormulation
        @test parse_acopf_formulation("acopf_sdp") isa ESFEX.SDPFormulation
        @test parse_acopf_formulation("acopf_polar") isa ESFEX.PolarNLPFormulation
        @test parse_acopf_formulation("acopf_rect") isa ESFEX.RectNLPFormulation
        @test_throws ErrorException parse_acopf_formulation("not_a_mode")
    end

    @testset "build_incidence_matrix" begin
        # Two lines on a 3-bus path: 1->2, 2->3.
        K = build_incidence_matrix(3, [(1, 2), (2, 3)])
        @test size(K) == (3, 2)
        # +1 leaving the "from" bus, -1 entering the "to" bus.
        @test K[1, 1] == 1.0 && K[2, 1] == -1.0
        @test K[2, 2] == 1.0 && K[3, 2] == -1.0
        # Bus not on a line has no entry; every column is balanced.
        @test K[3, 1] == 0.0
        @test all(==(0.0), sum(K, dims = 1))
        # No lines -> an (n, 0) matrix.
        @test size(build_incidence_matrix(4, Tuple{Int,Int}[])) == (4, 0)
    end

end
