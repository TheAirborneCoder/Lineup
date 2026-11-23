# SPDX-License-Identifier: GPL-3.0-or-later
"""Third stage of the groom name link: the most likely item for hair grooms that stage 2 leaves open.

Nothing here is proof, so nothing here names a groom. groom_names.py calls link(), which stores the most likely item
and its probability next to the candidates that stage 2 found.

Stage 2 (groom_align.py) keeps every way of giving each item consecutive grooms in archive order that the region,
rig and bone labels allow. This stage weighs those ways against each other.

Soft evidence, per item, from the words in its name (hairbob_fr1_003_wavy_l06_f01_sm_m_full):
  length code l01..l09   against the longest guide among the item's grooms
  style word             against the curl radius and two more style fields of the groom system page, and
                         against the presence of a braid part (system hash 0xe2500e92)
  fade code non/f01..f03 against the presence of a fuzz part (system hash 0x1caeb442)
  grooms per item        one shared distribution
The typical value for each word is fitted on the whole hair block (hard EM, about 40 numbers for 522 items). The
result is, for every groom, the probability of each item over all allowed alignments.

    python engine/groom_likely.py --test    # how well the probabilities hold where the answer is known

The test hides the rig labels inside the adult hair block, so that the 29 run boundaries the labels normally fix
become unknown, and compares where the model puts them with where they are.
"""
import collections
import re
import sys

import numpy as np

import groom_align as ga  # noqa: E402

CAP = 40                               # most grooms one item may own
PAGE_AT = ga.PAGE_AT                   # features() keeps system[PAGE_AT:PAGE_END] of every groom
STYLE_AT = (0x10A, 0x0F0, 0x130)       # half floats: curl radius and two fields that follow braided and curly
CLASS_AT = 0xCC                        # sixth hash of the system page: a part class, 30 values
BRAID, FUZZ = 0xE2500E92, 0x1CAEB442
SHOW = 0.5                             # a hint names the most likely item from this probability on
TEMPER = 0.6                           # weight of the soft evidence; 0.6 makes the probabilities hold in test()
NEG = -1e30
NAME = re.compile(r"(hair[a-z]+)_([a-z]{2}1)_\d{3}_([a-z]+)_l(\d\d)_([a-z0-9]+)_")


class Model:
    """ctx is what groom_align.link leaves behind: seq, sets, optional, rows, labels, allowed, pairs."""

    def __init__(self, ctx, free=False):
        seq, sets, rows = ctx["seq"], ctx["sets"], ctx["rows"]
        self.seq = seq
        self.n, self.m = len(seq), len(rows)
        words = {}
        self.code, self.word, self.fade, self.adult = (np.full(self.n, -1) for _ in range(4))
        for i, (name, labels) in enumerate(zip(seq, sets)):
            m = NAME.match(name or "")
            if m and labels <= {"sM", "sF"}:
                self.adult[i] = 1
                self.code[i] = int(m.group(4)) - 1
                self.word[i] = words.setdefault(m.group(3), len(words))
                self.fade[i] = int(m.group(5) != "non")
        self.adult = self.adult > 0
        self.words = words
        ok = np.array([[bool(it & gs) for gs in ctx["labels"]] for it in sets])
        if free:     # the test: forget rig and face/scalp labels inside the adult hair block
            head = np.array([bool(gs & ga.HEAD_LABELS) for gs in ctx["labels"]])
            ok[np.ix_(self.adult, head)] = True
        self.bad = np.concatenate((np.zeros((self.n, 1), np.int64), np.cumsum(~ok, axis=1)), axis=1)
        self.opt = np.zeros(self.n, bool)
        self.opt[ctx["optional"]] = True
        self.allowed, self.pairs = ctx["allowed"], ctx["pairs"]   # the split-chain rule of groom_align
        # per-groom evidence
        self.longest = np.array([f["longest"] for f in rows])
        style = np.full((self.m, len(STYLE_AT)), np.log(1e-4))
        cls = np.zeros(self.m, np.int64)
        for k, f in enumerate(rows):
            page = f["page"]
            for j, at in enumerate(STYLE_AT):
                v = float(np.frombuffer(page[at - PAGE_AT:at - PAGE_AT + 2], np.float16)[0])
                if np.isfinite(v):
                    style[k, j] = np.log(min(abs(v), 1e4) + 1e-4)
            cls[k] = int.from_bytes(page[CLASS_AT - PAGE_AT:CLASS_AT - PAGE_AT + 4], "little")
        self.style = style
        self.flag = np.stack((cls == BRAID, cls == FUZZ), axis=1).astype(float)
        # the same evidence for every run of 1..CAP grooms
        loglen = np.log(np.maximum(self.longest, 1e-3))
        wt = (self.longest > 0.03).astype(float)       # style fields of fuzz parts say nothing
        cw = np.concatenate(([0.0], np.cumsum(wt)))
        cs = np.concatenate((np.zeros((1, style.shape[1])), np.cumsum(style * wt[:, None], axis=0)))
        cf = np.concatenate((np.zeros((1, 2)), np.cumsum(self.flag, axis=0)))
        self.seg_len, self.seg_style, self.seg_has, self.seg_flag = {}, {}, {}, {}
        cur = loglen.copy()
        for ell in range(1, CAP + 1):
            if ell > 1:
                cur = np.maximum(cur[:-1], loglen[ell - 1:])
            a = np.arange(self.m - ell + 1)
            w = cw[a + ell] - cw[a]
            self.seg_len[ell] = cur.copy()
            self.seg_has[ell] = w > 0
            self.seg_style[ell] = (cs[a + ell] - cs[a]) / np.maximum(w, 1)[:, None]
            self.seg_flag[ell] = (cf[a + ell] - cf[a]) > 0
        # starting values; fit() replaces them
        self.mu = np.log(np.array([0.03, 0.05, 0.09, 0.15, 0.22, 0.28, 0.33, 0.40, 0.50]))
        self.sigma = 0.45
        self.nu = np.tile((style * wt[:, None]).sum(0) / max(wt.sum(), 1), (len(words), 1))
        self.tau = np.maximum(style.std(0), 0.3) * 2.0
        self.braid = np.full(len(words), 0.1)          # P(item has a braid part | style word)
        self.fuzz = np.full(2, 0.1)                    # P(item has a fuzz part | fade code)
        self.lam = 3.3                                 # mean grooms per item, minus one
        self.temper = TEMPER

    def copy_parameters(self, other):
        for name in ("mu", "sigma", "nu", "tau", "braid", "fuzz", "lam"):
            setattr(self, name, getattr(other, name))
        return self

    def _pieces(self):
        by_code, by_word, by_fade = {}, {}, {}
        for ell in range(1, CAP + 1):
            x = self.seg_len[ell]
            by_code[ell] = -0.5 * ((x[None, :] - self.mu[:, None]) / self.sigma) ** 2 - np.log(self.sigma)
            s = self.seg_style[ell]
            z = -0.5 * (((s[None, :, :] - self.nu[:, None, :]) / self.tau) ** 2).sum(2)
            z = np.where(self.seg_has[ell][None, :], z, -1.0)
            has = self.seg_flag[ell]
            z = z + np.where(has[None, :, 0], np.log(self.braid)[:, None], np.log1p(-self.braid)[:, None])
            by_word[ell] = z
            by_fade[ell] = np.where(has[None, :, 1], np.log(self.fuzz)[:, None], np.log1p(-self.fuzz)[:, None])
        k = np.arange(CAP)
        logfact = np.concatenate(([0.0], np.cumsum(np.log(np.arange(1, CAP)))))
        return by_code, by_word, by_fade, k * np.log(self.lam) - self.lam - logfact

    def _score(self, i, ell, pieces):
        if not self.adult[i]:
            return np.zeros(self.m - ell + 1)
        by_code, by_word, by_fade, prior = pieces
        return self.temper * (by_code[ell][self.code[i]] + by_word[ell][self.word[i]] + by_fade[ell][self.fade[i]]
                              + prior[ell - 1])

    def run(self, best=False):
        """post[i, k]: probability that item i owns groom k (or, with best, the single best alignment)."""
        n, m, last = self.n, self.m, self.n - 1      # the last item is the open tail of non-stub grooms
        pieces = self._pieces()
        comb = np.maximum if best else np.logaddexp
        alpha = np.full((n, m + 1), NEG)             # alpha[i, a]: items before i own grooms [0, a)
        alpha[0, 0] = 0.0
        for i in range(last):
            nxt = np.full(m + 1, NEG)
            cur = np.where(self.allowed, alpha[i], NEG)
            for ell in range(1, CAP + 1):
                a = np.arange(m - ell + 1)
                valid = self.bad[i, a + ell] == self.bad[i, a]
                if not valid.any():
                    break
                nxt[ell:] = comb(nxt[ell:], np.where(valid, cur[a] + self._score(i, ell, pieces), NEG))
            alpha[i + 1] = comb(nxt, cur) if self.opt[i] else nxt
        end = np.where(self.allowed, alpha[last], NEG)
        total = end.max() if best else np.logaddexp.reduce(end)
        post = np.zeros((n, m))
        starts = np.zeros(m + 1)                     # P(an item's grooms start at groom k)
        nxt = np.where(self.allowed, 0.0, NEG)       # beta of the item after i
        for i in range(last - 1, -1, -1):
            cur = np.full(m + 1, NEG)
            diff = np.zeros(m + 1)
            left = np.where(self.allowed, alpha[i], NEG)
            for ell in range(1, CAP + 1):
                a = np.arange(m - ell + 1)
                valid = self.bad[i, a + ell] == self.bad[i, a]
                if not valid.any():
                    break
                s = np.where(valid, self._score(i, ell, pieces) + nxt[a + ell], NEG)
                cur[:m - ell + 1] = comb(cur[:m - ell + 1], s)
                w = left[a] + s - total
                w = (w > -1e-6).astype(float) if best else np.exp(np.minimum(w, 0.0))
                diff[:m - ell + 1] += w
                diff[ell:] -= w
                starts[:m - ell + 1] += w
            if self.opt[i]:
                cur = comb(cur, nxt)
            nxt = np.where(self.allowed, cur, NEG)
            post[i] = np.cumsum(diff)[:m]
        tail = (end - total > -1e-6).astype(float) if best else np.exp(np.minimum(end - total, 0.0))
        post[last] = np.cumsum(tail)[:m]
        self.start_p = np.minimum(starts[:m] + tail[:m], 1.0)
        return total, post

    def best_path(self):
        """owner[k]: the item that owns groom k in the single best alignment, found by backtracking.

        Unlike the argmax of run(best=True), this gives every groom exactly one item and every item one contiguous
        run of grooms, even where alignments tie. Scaling the scores (temper) does not change it.
        """
        n, m, last = self.n, self.m, self.n - 1
        pieces = self._pieces()
        alpha = np.full((n, m + 1), NEG)
        back = np.zeros((n, m + 1), np.int16)        # grooms taken by item i - 1 to reach state (i, b); 0 = none
        alpha[0, 0] = 0.0
        for i in range(last):
            cur = np.where(self.allowed, alpha[i], NEG)
            nxt = np.full(m + 1, NEG)
            take = np.zeros(m + 1, np.int16)
            for ell in range(1, CAP + 1):
                a = np.arange(m - ell + 1)
                valid = self.bad[i, a + ell] == self.bad[i, a]
                if not valid.any():
                    break
                cand = np.where(valid, cur[a] + self._score(i, ell, pieces), NEG)
                better = cand > nxt[ell:]
                nxt[ell:] = np.where(better, cand, nxt[ell:])
                take[ell:] = np.where(better, ell, take[ell:])
            if self.opt[i]:
                skip = cur > nxt
                nxt = np.where(skip, cur, nxt)
                take = np.where(skip, 0, take)
            alpha[i + 1], back[i + 1] = nxt, take
        end = np.where(self.allowed, alpha[last], NEG)
        b = int(end.argmax())
        owner = np.full(m, last)
        for i in range(last, 0, -1):
            ell = int(back[i, b])
            owner[b - ell:b] = i - 1
            b -= ell
        assert b == 0, "best path does not reach the first groom"
        return owner

    def fit(self, rounds=6):
        """Hard EM: best alignment, then the typical value of every word from the items as that alignment cuts them."""
        for _ in range(rounds):
            _, post = self.run(best=True)
            owner = post.argmax(0)
            seg = collections.defaultdict(list)
            for k, i in enumerate(owner):
                seg[int(i)].append(k)
            lens, feats, braid, fuzz = (collections.defaultdict(list) for _ in range(4))
            sizes = []
            for i, ks in seg.items():
                if not self.adult[i]:
                    continue
                lens[self.code[i]].append(np.log(max(self.longest[ks].max(), 1e-3)))
                sizes.append(len(ks))
                braid[self.word[i]].append(self.flag[ks, 0].any())
                fuzz[self.fade[i]].append(self.flag[ks, 1].any())
                long_ = [k for k in ks if self.longest[k] > 0.03]
                if long_:
                    feats[self.word[i]].append(self.style[long_].mean(0))
            for c, xs in lens.items():
                if len(xs) >= 3:
                    self.mu[c] = np.mean(xs)
            self.mu = np.maximum.accumulate(self.mu)             # a higher code never means shorter hair
            self.sigma = max(float(np.std([x - self.mu[c] for c, xs in lens.items() for x in xs])), 0.15)
            for w, fs in feats.items():
                if len(fs) >= 5:
                    self.nu[w] = np.mean(fs, axis=0)
            self.tau = np.maximum(np.std([f - self.nu[w] for w, fs in feats.items() for f in fs], axis=0), 0.3)
            for w, bs in braid.items():
                self.braid[w] = (sum(bs) + 1.0) / (len(bs) + 2.0)
            for f, bs in fuzz.items():
                self.fuzz[f] = (sum(bs) + 1.0) / (len(bs) + 2.0)
            self.lam = max(float(np.mean(sizes)) - 1.0, 0.2)
        return self

    def describe(self):
        inv = {v: k for k, v in self.words.items()}
        return [
            "typical longest guide by length code l01..l09: " + " ".join(f"{np.exp(v) * 100:.0f}" for v in self.mu)
            + f" cm (spread x{np.exp(self.sigma):.2f})",
            "items with a braid part by style word: "
            + ", ".join(f"{inv[w]} {self.braid[w]:.0%}" for w in range(len(inv))),
            f"items with a fuzz part: {self.fuzz[0]:.0%} without a fade code, {self.fuzz[1]:.0%} with one",
            "typical curl radius by style word: "
            + ", ".join(f"{inv[w]} {(np.exp(self.nu[w][0]) - 1e-4) * 1000:.1f} mm" for w in range(len(inv))),
        ]


def link(ctx, names, hints, likely):
    """Fill likely[id] = (item, probability, group, start probability) for adult hair grooms.

    The item is the groom's owner in the single best alignment, so the grooms of one item form one contiguous
    group (group = archive position of its first groom) and no groom is left out of a group. The probability is
    the groom's own probability for that item over all alignments. The start probability says how likely an item
    starts at this groom, which marks the uncertain edges of a group.
    """
    if not ctx:
        return ["third stage skipped: the second stage found no alignment"]
    model = Model(ctx).fit()
    _, post = model.run()
    owner = model.best_path()
    first = {}
    for k, i in enumerate(owner):
        first.setdefault(int(i), ctx["rows"][k]["ord"])
    shown = sure = 0
    for k, f in enumerate(ctx["rows"]):
        i = int(owner[k])
        if not model.adult[i] or f["id"] in names:
            continue
        p = min(float(post[i, k]), 0.999)          # an estimate, never shown as certain
        likely[f["id"]] = (ctx["seq"][i], p, first[i], min(float(model.start_p[k]), 0.999))
        if p >= SHOW:
            share = "over 99%" if p >= 0.995 else f"{p:.0%}"
            hints[f["id"]] = f"most likely {ctx['seq'][i]} ({share}, an estimate); " + hints.get(f["id"], "")
            shown += 1
            sure += p >= 0.9
    groups = collections.Counter(v[2] for v in likely.values())
    return [f"most likely item (an estimate, not a name): {len(likely)} adult hair grooms in {len(groups)} groups "
            f"from the best alignment, {shown} rated 50% or more, {sure} at 90% or more"] + model.describe()


def test():
    """Hide the rig labels inside the adult hair block and see where the model puts the run boundaries they fix."""
    import groom_names as gn
    items, assets = gn.load()
    names, hints, _ = gn.link(items, assets)
    ctx = {}
    ga.link(items, assets, names, hints, ctx)
    model = Model(ctx).fit()
    for line in model.describe():
        print(line)
    free = Model(ctx, free=True).copy_parameters(model)
    free.temper = float(next((a.split('=')[1] for a in sys.argv if a.startswith('--temper=')), TEMPER))
    print('weight of the soft evidence', free.temper)
    owner = free.best_path()
    free_post = free.run()[1]
    # rig runs of adult hair items, and the grooms the hard rules pin to one run
    runs, prev = [], None
    for i in range(model.n):
        key = NAME.match(ctx["seq"][i]).group(1, 2) if model.adult[i] else None
        if key and key == prev:
            runs[-1].append(i)
        elif key:
            runs.append([i])
        prev = key
    run_of = {i: r for r, run in enumerate(runs) for i in run}
    possible = ctx["possible"]
    truth = {}
    for k in range(model.m):
        rs = {run_of.get(int(i), -1) for i in np.nonzero(possible[:, k])[0]}
        if len(rs) == 1 and -1 not in rs:
            truth[k] = next(iter(rs))
    errs, sides = [], []
    for r in range(len(runs) - 1):
        left = [k for k, v in truth.items() if v == r]
        right = [k for k, v in truth.items() if v == r + 1]
        if runs[r][-1] + 1 != runs[r + 1][0] or not left or not right:
            continue                           # a child run sits between and still pins the boundary
        lo, hi = max(left) + 1, min(right)     # the boundary is known to lie in [lo, hi]
        if hi - lo > 1:
            continue                           # too loose to score a hit
        pred = int(np.searchsorted(owner, runs[r + 1][0]))
        err = 0 if lo <= pred <= hi else (pred - lo if pred < lo else pred - hi)
        errs.append(err)
        for k in list(range(lo - 3, lo)) + list(range(hi, hi + 3)):
            if k in truth:
                run = runs[truth[k]]
                sides.append((float(free_post[run[0]:run[-1] + 1, k].sum()), run_of.get(int(owner[k]), -1) == truth[k]))
        print(f"  {ctx['seq'][runs[r][-1]][:30]:30s} | {ctx['seq'][runs[r + 1][0]][:30]:30s} off by {err:+d}")
    errs, sides = np.abs(np.array(errs)), np.array(sides)
    print(f"{len(errs)} known boundaries: {int((errs == 0).sum())} exact, {int((errs <= 1).sum())} within 1 groom, "
          f"{int((errs <= 2).sum())} within 2, worst {int(errs.max())}")
    print(f"{len(sides)} grooms next to those boundaries, {int(sides[:, 1].sum())} put in the right run")
    for lo_, hi_ in ((0.0, 0.5), (0.5, 0.75), (0.75, 0.9), (0.9, 0.99), (0.99, 1.01)):
        sel = (sides[:, 0] >= lo_) & (sides[:, 0] < hi_)
        if sel.any():
            print(f"  probability of the right run {lo_:.2f}-{min(hi_, 1):.2f}: {int(sel.sum())} grooms, "
                  f"best alignment right for {sides[sel, 1].mean():.0%}")
    # the probability the model gives its own pick, against how often that pick is in the right run
    for lo_, hi_ in ((0.5, 0.75), (0.75, 0.9), (0.9, 0.99), (0.99, 1.01)):
        hit = n = 0
        for k, r in truth.items():
            i = int(free_post[:-1, k].argmax())
            p = free_post[i, k]
            near = any(abs(k - e) <= 6 for e in edges(truth, runs))
            if lo_ <= p < hi_ and model.adult[i] and near:
                n += 1
                hit += run_of.get(i, -1) == r
        if n:
            print(f"  most likely item rated {lo_:.2f}-{min(hi_, 1):.2f}, within 6 grooms of a known boundary: "
                  f"{n} grooms, item in the right run for {hit / n:.0%}")


def edges(truth, runs, cache={}):
    if not cache:
        last = collections.defaultdict(int)
        for k, r in truth.items():
            last[r] = max(last[r], k)
        cache["e"] = sorted(v + 1 for v in last.values())
    return cache["e"]


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
    else:
        print(__doc__)
