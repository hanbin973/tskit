# MIT License
#
# Copyright (c) 2024 Tskit Developers
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
Test cases for matrix-vector product stats
"""
import msprime
import numpy as np
import pytest

import tskit
from tests.test_highlevel import get_example_tree_sequences

# ↑ See https://github.com/tskit-dev/tskit/issues/1804 for when
# we can remove this.


# Implementation note: the class structure here, where we pass in all the
# needed arrays through the constructor was determined by an older version
# in which we used numba acceleration. We could just pass in a reference to
# the tree sequence now, but it is useful to keep track of exactly what we
# require, so leaving it as it is for now.
class RelatednessVector:
    def __init__(
        self,
        sample_weights,
        windows,
        num_nodes,
        samples,
        nodes_time,
        edges_left,
        edges_right,
        edges_parent,
        edges_child,
        edge_insertion_order,
        edge_removal_order,
        sequence_length,
        verbosity=0,
        internal_checks=False,
        centre=True,
    ):
        self.sample_weights = np.asarray(sample_weights, dtype=np.float64)
        self.num_weights = self.sample_weights.shape[1]
        self.windows = windows
        N = num_nodes
        self.parent = np.full(N, -1, dtype=np.int32)
        # Edges and indexes
        self.edges_left = edges_left
        self.edges_right = edges_right
        self.edges_parent = edges_parent
        self.edges_child = edges_child
        self.edge_insertion_order = edge_insertion_order
        self.edge_removal_order = edge_removal_order
        self.sequence_length = sequence_length
        self.nodes_time = nodes_time
        self.samples = samples
        self.position = 0.0
        self.x = np.zeros(N, dtype=np.float64)
        self.w = np.zeros((N, self.num_weights), dtype=np.float64)
        self.v = np.zeros((N, self.num_weights), dtype=np.float64)
        self.verbosity = verbosity
        self.internal_checks = internal_checks
        self.centre = centre

        if self.centre:
            self.sample_weights -= np.mean(self.sample_weights, axis=0)

        for j, u in enumerate(samples):
            self.w[u] = self.sample_weights[j]

        if self.verbosity > 0:
            self.print_state("init")

    def print_state(self, msg=""):
        num_nodes = len(self.parent)
        print(f"..........{msg}................")
        print(f"position = {self.position}")
        for j in range(num_nodes):
            st = f"{self.nodes_time[j]}"
            pt = (
                "NaN"
                if self.parent[j] == tskit.NULL
                else f"{self.nodes_time[self.parent[j]]}"
            )
            print(
                f"node {j} -> {self.parent[j]}: "
                f"z = ({pt} - {st})"
                f" * ({self.position} - {self.x[j]:.2})"
                f" * {','.join(map(str, self.w[j].round(2)))}"
                f" = {','.join(map(str, self.get_z(j).round(2)))}"
            )
            print(f"         value: {','.join(map(str, self.v[j].round(2)))}")
        roots = []
        fmt = "{:<6}{:>8}\t{}\t{}\t{}"
        s = f"roots = {roots}\n"
        s += (
            fmt.format(
                "node",
                "parent",
                "value",
                "weight",
                "z",
            )
            + "\n"
        )
        for u in range(num_nodes):
            u_str = f"{u}"
            s += (
                fmt.format(
                    u_str,
                    self.parent[u],
                    ",".join(map(str, self.v[u].round(2))),
                    ",".join(map(str, self.w[u].round(2))),
                    ",".join(map(str, self.get_z(u).round(2))),
                )
                + "\n"
            )
        print(s)

        print("Current state:")
        state = self.current_state()
        for j, x in enumerate(state):
            print(f"   {j}: {x}")
        print("..........................")

    def remove_edge(self, p, c):
        if self.verbosity > 0:
            self.print_state(f"remove {int(p), int(c)}")
        assert p != -1
        self.v[c] += self.get_z(c)
        self.x[c] = self.position
        self.parent[c] = -1
        self.adjust_path_up(p, c, -1)

    def insert_edge(self, p, c):
        if self.verbosity > 0:
            self.print_state(f"insert {int(p), int(c)}")
        assert p != -1
        assert self.parent[c] == -1, "contradictory edges"
        self.adjust_path_up(p, c, +1)
        self.x[c] = self.position
        self.parent[c] = p

    def adjust_path_up(self, p, c, sign):
        # sign = -1 for removing edges, +1 for adding
        while p != tskit.NULL:
            self.v[p] += self.get_z(p)
            self.x[p] = self.position
            self.v[c] -= sign * self.v[p]
            self.w[p] += sign * self.w[c]
            p = self.parent[p]

    def get_z(self, u):
        p = self.parent[u]
        if p == tskit.NULL:
            return np.zeros(self.num_weights, dtype=np.float64)
        time = self.nodes_time[p] - self.nodes_time[u]
        span = self.position - self.x[u]
        return time * span * self.w[u]

    def mrca(self, a, b):
        # just used for `current_state`
        aa = [a]
        while a != tskit.NULL:
            a = self.parent[a]
            aa.append(a)
        while b not in aa:
            b = self.parent[b]
        return b

    def write_output(self):
        """
        Compute and return the current state, zero-ing out
        all contributions (used for switching between windows).
        """
        n = len(self.samples)
        out = np.zeros((n, self.num_weights))
        for j, c in enumerate(self.samples):
            while c != tskit.NULL:
                if self.x[c] != self.position:
                    self.v[c] += self.get_z(c)
                    self.x[c] = self.position
                out[j] += self.v[c]
                c = self.parent[c]
        self.v *= 0.0
        return out

    def current_state(self):
        """
        Compute the current output, for debugging.
        """
        if self.verbosity > 2:
            print("---------------")
        n = len(self.samples)
        out = np.zeros((n, self.num_weights))
        for j, a in enumerate(self.samples):
            # edges on the path up from a
            pa = a
            while pa != tskit.NULL:
                if self.verbosity > 2:
                    print("edge:", pa, self.get_z(pa))
                out[j] += self.get_z(pa) + self.v[pa]
                pa = self.parent[pa]
        if self.verbosity > 2:
            print("---------------")
        return out

    def run(self):
        M = self.edges_left.shape[0]
        in_order = self.edge_insertion_order
        out_order = self.edge_removal_order
        edges_left = self.edges_left
        edges_right = self.edges_right
        edges_parent = self.edges_parent
        edges_child = self.edges_child
        num_windows = len(self.windows) - 1
        out = np.zeros((num_windows,) + self.sample_weights.shape)

        j = 0
        k = 0
        m = 0
        self.position = 0

        while m < num_windows and k < M and self.position <= self.sequence_length:
            while k < M and edges_right[out_order[k]] == self.position:
                p = edges_parent[out_order[k]]
                c = edges_child[out_order[k]]
                self.remove_edge(p, c)
                k += 1
            while j < M and edges_left[in_order[j]] == self.position:
                p = edges_parent[in_order[j]]
                c = edges_child[in_order[j]]
                self.insert_edge(p, c)
                assert self.parent[p] == tskit.NULL or self.x[p] == self.position
                j += 1
            right = self.windows[m + 1]
            if j < M:
                right = min(right, edges_left[in_order[j]])
            if k < M:
                right = min(right, edges_right[out_order[k]])
            self.position = right
            if self.position == self.windows[m + 1]:
                out[m] = self.write_output()
                m = m + 1

        if self.verbosity > 1:
            self.print_state()

        if self.centre:
            for m in range(num_windows):
                out[m] -= np.mean(out[m], axis=0)
        return out


def relatedness_vector(ts, sample_weights, windows=None, **kwargs):
    if len(sample_weights.shape) == 1:
        sample_weights = sample_weights[:, np.newaxis]
    drop_dimension = windows is None
    if drop_dimension:
        windows = [0, ts.sequence_length]
    rv = RelatednessVector(
        sample_weights,
        windows,
        ts.num_nodes,
        samples=ts.samples(),
        nodes_time=ts.nodes_time,
        edges_left=ts.edges_left,
        edges_right=ts.edges_right,
        edges_parent=ts.edges_parent,
        edges_child=ts.edges_child,
        edge_insertion_order=ts.indexes_edge_insertion_order,
        edge_removal_order=ts.indexes_edge_removal_order,
        sequence_length=ts.sequence_length,
        **kwargs,
    )
    out = rv.run()
    if drop_dimension:
        assert len(out.shape) == 3 and out.shape[0] == 1
        out = out[0]
    return out


def relatedness_matrix(ts, windows, centre):
    Sigma = ts.genetic_relatedness(
        sample_sets=[[i] for i in ts.samples()],
        indexes=[(i, j) for i in range(ts.num_samples) for j in range(ts.num_samples)],
        windows=windows,
        mode="branch",
        span_normalise=False,
        proportion=False,
        centre=centre,
    )
    if windows is not None:
        shape = (len(windows) - 1, ts.num_samples, ts.num_samples)
    else:
        shape = (ts.num_samples, ts.num_samples)
    return Sigma.reshape(shape)


def verify_relatedness_vector(
    ts, w, windows, *, internal_checks=False, verbosity=0, centre=True
):
    R1 = relatedness_vector(
        ts,
        sample_weights=w,
        windows=windows,
        internal_checks=internal_checks,
        verbosity=verbosity,
        centre=centre,
    )
    wvec = w if len(w.shape) > 1 else w[:, np.newaxis]
    Sigma = relatedness_matrix(ts, windows=windows, centre=centre)
    if windows is None:
        R2 = Sigma.dot(wvec)
    else:
        R2 = np.zeros((len(windows) - 1, ts.num_samples, wvec.shape[1]))
        for k in range(len(windows) - 1):
            R2[k] = Sigma[k].dot(wvec)
    R3 = ts.genetic_relatedness_vector(w, windows=windows, mode="branch", centre=centre)
    if verbosity > 0:
        print(ts.draw_text())
        print("weights:", w)
        print("windows:", windows)
        print("here:", R1)
        print("with ts:", R2)
        print("with lib:", R3)
        print("Sigma:", Sigma)
    if windows is None:
        assert R1.shape == (ts.num_samples, wvec.shape[1])
    else:
        assert R1.shape == (len(windows) - 1, ts.num_samples, wvec.shape[1])
    np.testing.assert_allclose(R1, R2, atol=1e-13)
    np.testing.assert_allclose(R1, R3, atol=1e-13)
    return R1


def check_relatedness_vector(
    ts, n=2, num_windows=0, *, internal_checks=False, verbosity=0, seed=123, centre=True
):
    rng = np.random.default_rng(seed=seed)
    if num_windows == 0:
        windows = None
    else:
        windows = np.linspace(0, ts.sequence_length, num_windows + 1)
    for k in range(n):
        if k == 0:
            w = rng.normal(size=ts.num_samples)
        else:
            w = rng.normal(size=ts.num_samples * k).reshape((ts.num_samples, k))
        w = np.round(len(w) * w)
        R = verify_relatedness_vector(
            ts,
            w,
            windows,
            internal_checks=internal_checks,
            verbosity=verbosity,
            centre=centre,
        )
    return R


class TestExamples:

    def test_bad_weights(self):
        n = 5
        ts = msprime.sim_ancestry(
            n,
            ploidy=2,
            sequence_length=10,
            random_seed=123,
        )
        for bad_W in (None, [1], np.ones((3 * n, 2)), np.ones((n - 1, 2))):
            with pytest.raises(ValueError, match="number of samples"):
                ts.genetic_relatedness_vector(bad_W, mode="branch")

    def test_bad_windows(self):
        n = 5
        ts = msprime.sim_ancestry(
            n,
            ploidy=2,
            sequence_length=10,
            random_seed=123,
        )
        for bad_w in ([1], []):
            with pytest.raises(ValueError, match="Windows array"):
                ts.genetic_relatedness_vector(
                    np.ones(ts.num_samples), windows=bad_w, mode="branch"
                )

    @pytest.mark.parametrize("n", [2, 3, 5])
    @pytest.mark.parametrize("seed", range(1, 4))
    @pytest.mark.parametrize("centre", (True, False))
    @pytest.mark.parametrize("num_windows", (0, 1, 2))
    def test_small_internal_checks(self, n, seed, centre, num_windows):
        ts = msprime.sim_ancestry(
            n,
            ploidy=1,
            sequence_length=1000,
            recombination_rate=0.01,
            random_seed=seed,
        )
        assert ts.num_trees >= 2
        check_relatedness_vector(ts, internal_checks=True, centre=centre)

    @pytest.mark.parametrize("n", [2, 3, 5, 15])
    @pytest.mark.parametrize("seed", range(1, 5))
    @pytest.mark.parametrize("centre", (True, False))
    @pytest.mark.parametrize("num_windows", (0, 1, 3))
    def test_simple_sims(self, n, seed, centre, num_windows):
        ts = msprime.sim_ancestry(
            n,
            ploidy=1,
            population_size=20,
            sequence_length=100,
            recombination_rate=0.01,
            random_seed=seed,
        )
        assert ts.num_trees >= 2
        check_relatedness_vector(
            ts, num_windows=num_windows, centre=centre, verbosity=0
        )

    @pytest.mark.parametrize("n", [2, 3, 5, 15])
    @pytest.mark.parametrize("centre", (True, False))
    def test_single_balanced_tree(self, n, centre):
        ts = tskit.Tree.generate_balanced(n).tree_sequence
        check_relatedness_vector(ts, internal_checks=True, centre=centre)

    @pytest.mark.parametrize("centre", (True, False))
    def test_internal_sample(self, centre):
        tables = tskit.Tree.generate_balanced(4).tree_sequence.dump_tables()
        flags = tables.nodes.flags
        flags[3] = 0
        flags[5] = tskit.NODE_IS_SAMPLE
        tables.nodes.flags = flags
        ts = tables.tree_sequence()
        check_relatedness_vector(ts, centre=centre)

    @pytest.mark.parametrize("seed", range(1, 5))
    @pytest.mark.parametrize("centre", (True, False))
    @pytest.mark.parametrize("num_windows", (0, 1, 2))
    def test_one_internal_sample_sims(self, seed, centre, num_windows):
        ts = msprime.sim_ancestry(
            10,
            ploidy=1,
            population_size=20,
            sequence_length=100,
            recombination_rate=0.01,
            random_seed=seed,
        )
        t = ts.dump_tables()
        # Add a new sample directly below another sample
        u = t.nodes.add_row(time=-1, flags=tskit.NODE_IS_SAMPLE)
        t.edges.add_row(parent=0, child=u, left=0, right=ts.sequence_length)
        t.sort()
        t.build_index()
        ts = t.tree_sequence()
        check_relatedness_vector(ts, num_windows=num_windows, centre=centre)

    @pytest.mark.parametrize("centre", (True, False))
    @pytest.mark.parametrize("num_windows", (0, 1, 2))
    def test_missing_flanks(self, centre, num_windows):
        ts = msprime.sim_ancestry(
            2,
            ploidy=1,
            population_size=10,
            sequence_length=100,
            recombination_rate=0.001,
            random_seed=1234,
        )
        assert ts.num_trees >= 2
        ts = ts.keep_intervals([[20, 80]])
        assert ts.first().interval == (0, 20)
        check_relatedness_vector(ts, num_windows=num_windows, centre=centre)

    @pytest.mark.parametrize("ts", get_example_tree_sequences())
    @pytest.mark.parametrize("centre", (True, False))
    def test_suite_examples(self, ts, centre):
        if ts.num_samples > 0:
            check_relatedness_vector(ts, centre=centre)

    @pytest.mark.parametrize("n", [2, 3, 10])
    def test_dangling_on_samples(self, n):
        # Adding non sample branches below the samples does not alter
        # the overall divergence *between* the samples
        ts1 = tskit.Tree.generate_balanced(n).tree_sequence
        D1 = check_relatedness_vector(ts1)
        tables = ts1.dump_tables()
        for u in ts1.samples():
            v = tables.nodes.add_row(time=-1)
            tables.edges.add_row(left=0, right=ts1.sequence_length, parent=u, child=v)
        tables.sort()
        tables.build_index()
        ts2 = tables.tree_sequence()
        D2 = check_relatedness_vector(ts2, internal_checks=True)
        np.testing.assert_array_almost_equal(D1, D2)

    @pytest.mark.parametrize("n", [2, 3, 10])
    @pytest.mark.parametrize("centre", (True, False))
    def test_dangling_on_all(self, n, centre):
        # Adding non sample branches below the samples does not alter
        # the overall divergence *between* the samples
        ts1 = tskit.Tree.generate_balanced(n).tree_sequence
        D1 = check_relatedness_vector(ts1, centre=centre)
        tables = ts1.dump_tables()
        for u in range(ts1.num_nodes):
            v = tables.nodes.add_row(time=-1)
            tables.edges.add_row(left=0, right=ts1.sequence_length, parent=u, child=v)
        tables.sort()
        tables.build_index()
        ts2 = tables.tree_sequence()
        D2 = check_relatedness_vector(ts2, internal_checks=True, centre=centre)
        np.testing.assert_array_almost_equal(D1, D2)

    @pytest.mark.parametrize("centre", (True, False))
    def test_disconnected_non_sample_topology(self, centre):
        # Adding non sample branches below the samples does not alter
        # the overall divergence *between* the samples
        ts1 = tskit.Tree.generate_balanced(5).tree_sequence
        D1 = check_relatedness_vector(ts1, centre=centre)
        tables = ts1.dump_tables()
        # Add an extra bit of disconnected non-sample topology
        u = tables.nodes.add_row(time=0)
        v = tables.nodes.add_row(time=1)
        tables.edges.add_row(left=0, right=ts1.sequence_length, parent=v, child=u)
        tables.sort()
        tables.build_index()
        ts2 = tables.tree_sequence()
        D2 = check_relatedness_vector(ts2, internal_checks=True, centre=centre)
        np.testing.assert_array_almost_equal(D1, D2)