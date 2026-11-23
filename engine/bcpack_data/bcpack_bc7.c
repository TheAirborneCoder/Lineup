/* BC7 block rebuild from BCPack lane symbols (clean-room; see bcpack.py for the format description and
   bcpack_research/README.md section 8 for how each rule was found and checked).
   Build (MSVC):  cl /nologo /O2 /LD bcpack_bc7.c /Fe:bcpack_bc7.dll
   Build (gcc):   gcc -O2 -shared -o bcpack_bc7.so bcpack_bc7.c */
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

/* status bits per block */
#define ST_EP_UNKNOWN   1    /* endpoints depend on a built-in history entry that was not supplied */
#define ST_IX_UNKNOWN   2    /* indices depend on a built-in history entry that was not supplied */
#define ST_CFG_UNKNOWN  4    /* mode / partition of this signature is not pinned */
#define ST_ANCHOR       8    /* an anchor index came out with its top bit set (model violation) */
#define ST_PBIT        16    /* shared p-bit differs between the two endpoints (model violation) */
#define ST_SIGNATURE   32    /* signature outside the table */

typedef struct {
    const uint8_t *cfg;        /* [286][4]: mode, partition (255 = unknown), rotation, index selection */
    const int16_t *rank_of;    /* [32*16] signature cell -> rank, -1 invalid */
    const uint8_t *p2, *p3;    /* [64][16] subset of each pixel */
    const uint8_t *a2, *a3a, *a3b; /* [64] anchors */
    const uint8_t *pre_ep;     /* [256][3][2][4] left-aligned endpoint state of the built-in history */
    const uint8_t *pre_ep_ok;  /* [256] */
    const uint8_t *pre_ix;     /* [256][2][16] left-aligned (4 bit) index state */
    const uint8_t *pre_ix_ok;  /* [256] */
    int32_t missing_last;      /* 0: a missing subset copies subset 0; 1: copies the last present subset */
    int32_t m5_swap;           /* 0: mode 5 colour indices come from lanes 6,7,8,10; 1: from lanes 11..14 */
} Params;

static const int NS[8]  = {3, 2, 3, 2, 1, 1, 1, 2};
static const int CB[8]  = {4, 6, 5, 7, 5, 7, 7, 5};
static const int AB[8]  = {0, 0, 0, 0, 6, 8, 7, 5};
static const int PK[8]  = {1, 2, 0, 1, 0, 0, 1, 1};   /* 0 none, 1 per endpoint, 2 shared per subset */
static const int IB[8]  = {3, 3, 2, 2, 3, 2, 4, 2};   /* bits of the index set in lanes 11..14 */
static const int PB[8]  = {4, 6, 6, 6, 0, 0, 0, 6};
/* lane of (subset, channel); -1 none */
static const int LANE[3][4] = {{2, 3, 4, 5}, {6, 7, 8, 9}, {10, 5, 9, -1}};

static int unzig(int v) { return (v & 1) ? -((v + 1) >> 1) : (v >> 1); }
static int inter8(int a, int b) {
    int d = 0;
    for (int k = 0; k < 4; k++) d |= ((a >> k) & 1) << (2 * k) | ((b >> k) & 1) << (2 * k + 1);
    return d;
}
static int deint(int s, int odd) {
    int v = 0;
    for (int k = 0; k < 4; k++) v |= ((s >> (2 * k + odd)) & 1) << k;
    return v;
}

typedef struct { uint64_t lo, hi; int pos; } Bits;
static void put(Bits *b, uint32_t v, int n) {
    if (n <= 0) return;
    uint64_t x = (uint64_t)(v & ((n >= 32) ? 0xFFFFFFFFu : ((1u << n) - 1)));
    if (b->pos < 64) {
        b->lo |= x << b->pos;
        if (b->pos + n > 64) b->hi |= x >> (64 - b->pos);
    } else b->hi |= x << (b->pos - 64);
    b->pos += n;
}

/* syms: uint16 [16][2*n] lane-major. out: [n][16]. status: [n]. ep_root / ix_root: int16 [n]
   (-1 chain ends without reference, 0..255 the built-in entry the chain starts from).
   fields (optional, may be NULL): int16 [n][64]: mode, part, rot, isel, then real 8-bit endpoints [3][2][4] (24),
   index set 1 [16], index set 2 [16], 4 spare. Returns the number of blocks whose status is 0. */
EXPORT int bc7_rebuild(const uint16_t *syms, int n, const Params *P, uint8_t *out, uint8_t *status,
                       int16_t *ep_root, int16_t *ix_root, int16_t *fields)
{
    /* per-block state kept for the whole chunk (references reach 255 blocks back only, but keeping all is simpler) */
    static uint8_t *ep = 0, *ix = 0; static uint8_t *known = 0; static int cap = 0;
    if (n > cap) {
        free(ep); free(ix); free(known);
        ep = (uint8_t *)malloc((size_t)n * 24); ix = (uint8_t *)malloc((size_t)n * 32); known = (uint8_t *)malloc((size_t)n * 2); cap = n;
    }
    int good = 0;
    for (int i = 0; i < n; i++) {
        int A[16], B[16];
        for (int l = 0; l < 16; l++) { A[l] = syms[(size_t)l * 2 * n + 2 * i]; B[l] = syms[(size_t)l * 2 * n + 2 * i + 1]; }
        uint8_t st = 0;
        uint8_t *sep = ep + (size_t)i * 24, *six = ix + (size_t)i * 32;
        memset(out + (size_t)i * 16, 0, 16);
        int rank = (A[15] < 32 && B[15] < 16) ? P->rank_of[A[15] * 16 + B[15]] : -1;
        if (rank < 0 || rank > 285) {
            status[i] = ST_SIGNATURE; memset(sep, 0, 24); memset(six, 0, 32); known[2 * i] = known[2 * i + 1] = 0;
            ep_root[i] = ix_root[i] = -2; continue;
        }
        int mode = P->cfg[rank * 4], part = P->cfg[rank * 4 + 1], rot = P->cfg[rank * 4 + 2], isel = P->cfg[rank * 4 + 3];
        if (mode > 7) {
            status[i] = ST_SIGNATURE; memset(sep, 0, 24); memset(six, 0, 32); known[2 * i] = known[2 * i + 1] = 0;
            ep_root[i] = ix_root[i] = -2; continue;
        }
        if (part == 255) st |= ST_CFG_UNKNOWN;
        int di = inter8(A[1], B[1]);
        int de = (di + unzig(inter8(A[0], B[0]))) & 255;
        /* ---- endpoint reference ---- */
        uint8_t zero_ep[24]; const uint8_t *rep; int ep_ok = 1;
        memset(zero_ep, 0, 24);
        if (de == 0) { rep = zero_ep; ep_root[i] = -1; }
        else if (i >= de) { rep = ep + (size_t)(i - de) * 24; ep_ok = known[2 * (i - de)]; ep_root[i] = ep_root[i - de]; }
        else { int k = 256 + i - de; rep = P->pre_ep + (size_t)k * 24; ep_ok = P->pre_ep_ok[k]; ep_root[i] = (int16_t)k; }
        int ns = NS[mode], cb = CB[mode], ab = AB[mode], pk = PK[mode];
        int w[4] = {cb, cb, cb, ab};
        if (rot) { w[rot - 1] = ab; w[3] = cb; }
        int val[3][2][4]; memset(val, 0, sizeof(val));
        for (int s = 0; s < ns; s++)
            for (int c = 0; c < 4; c++) {
                int lane = LANE[s][c];
                if (w[c] == 0 || lane < 0) continue;
                int bits = w[c] + ((pk && c == 0) ? 1 : 0);
                for (int e = 0; e < 2; e++) {
                    int pred = rep[s * 8 + e * 4 + c] >> (8 - bits);
                    val[s][e][c] = (pred + unzig(e ? B[lane] : A[lane])) & ((1 << bits) - 1);
                }
            }
        int pb[3][2] = {{0, 0}, {0, 0}, {0, 0}};
        for (int s = 0; s < ns; s++) {
            if (pk == 1) { pb[s][0] = val[s][0][0] & 1; pb[s][1] = val[s][1][0] & 1; }
            else if (pk == 2) { pb[s][0] = pb[s][1] = val[s][0][0] & 1; if ((val[s][1][0] & 1) != pb[s][0]) st |= ST_PBIT; }
            for (int e = 0; e < 2; e++)
                for (int c = 0; c < 4; c++) {
                    if (w[c] == 0) { sep[s * 8 + e * 4 + c] = 0; continue; }   /* a channel the mode lacks leaves 0 */
                    int bits = w[c] + ((pk && c == 0) ? 1 : 0);
                    sep[s * 8 + e * 4 + c] = (uint8_t)(val[s][e][c] << (8 - bits));
                }
        }
        for (int s = ns; s < 3; s++) memcpy(sep + s * 8, sep + (P->missing_last ? (ns - 1) : 0) * 8, 8);
        known[2 * i] = (uint8_t)ep_ok;
        if (!ep_ok) st |= ST_EP_UNKNOWN;
        /* ---- index reference ---- */
        uint8_t zero_ix[32]; const uint8_t *rix; int ix_ok = 1;
        memset(zero_ix, 0, 32);
        if (di == 0) { rix = zero_ix; ix_root[i] = -1; }
        else if (i >= di) { rix = ix + (size_t)(i - di) * 32; ix_ok = known[2 * (i - di) + 1]; ix_root[i] = ix_root[i - di]; }
        else { int k = 256 + i - di; rix = P->pre_ix + (size_t)k * 32; ix_ok = P->pre_ix_ok[k]; ix_root[i] = (int16_t)k; }
        int b1 = IB[mode], two = (mode == 4 || mode == 5);
        int s1[16], s2[16];
        static const int L1[4] = {11, 12, 13, 14}, L2[4] = {6, 7, 8, 10};
        for (int p = 0; p < 16; p++) {
            int y = p >> 2, x = p & 3, el = x >> 1, first = !(x & 1);
            int odd = (y < 3) ? first : !first;
            int sym = el ? B[L1[y]] : A[L1[y]];
            s1[p] = ((rix[p] >> (4 - b1)) + unzig(deint(sym, odd))) & ((1 << b1) - 1);
            six[p] = (uint8_t)(s1[p] << (4 - b1));
            if (two) {
                int sym2 = el ? B[L2[y]] : A[L2[y]];
                s2[p] = ((rix[16 + p] >> 2) + unzig(deint(sym2, odd))) & 3;
                six[16 + p] = (uint8_t)(s2[p] << 2);
            } else { s2[p] = 0; six[16 + p] = 0; }
        }
        known[2 * i + 1] = (uint8_t)ix_ok;
        if (!ix_ok) st |= ST_IX_UNKNOWN;
        /* ---- assemble ---- */
        int usepart = (part == 255) ? 0 : part;
        const uint8_t *sm = (ns == 2) ? P->p2 + usepart * 16 : (ns == 3) ? P->p3 + usepart * 16 : 0;
        int anc[3] = {0, 0, 0};
        if (ns == 2) anc[1] = P->a2[usepart];
        if (ns == 3) { anc[1] = P->a3a[usepart]; anc[2] = P->a3b[usepart]; }
        /* block index sets */
        int *pri, *sec; int ib, ib2;
        if (mode == 4) { pri = s2; ib = 2; sec = s1; ib2 = 3; }
        else if (mode == 5) { if (P->m5_swap) { pri = s1; sec = s2; } else { pri = s2; sec = s1; } ib = 2; ib2 = 2; }
        else { pri = s1; ib = b1; sec = 0; ib2 = 0; }
        int allzero = 1;
        for (int s = 0; s < ns && allzero; s++) for (int e = 0; e < 2 && allzero; e++) for (int c = 0; c < 4; c++) if (val[s][e][c]) { allzero = 0; break; }
        for (int p = 0; p < 16 && allzero; p++) if (s1[p] || s2[p]) allzero = 0;
        if (mode == 0 && part == 0 && allzero) {
            /* the all-zero block (padding) is stored as mode 0, partition 0 with every field zero */
        } else {
            Bits bw = {0, 0, 0};
            put(&bw, 1u << mode, mode + 1);
            put(&bw, (uint32_t)usepart, PB[mode]);
            if (mode == 4 || mode == 5) put(&bw, (uint32_t)rot, 2);
            if (mode == 4) put(&bw, (uint32_t)isel, 1);
            /* block-order fields: with a rotation the block's colour slot rot-1 holds true alpha and its alpha slot holds that colour */
            for (int c = 0; c < 3; c++) {
                int tc = (rot && c == rot - 1) ? 3 : c;
                for (int s = 0; s < ns; s++) for (int e = 0; e < 2; e++) {
                    int v = val[s][e][tc];
                    if (pk && tc == 0) v >>= 1;
                    put(&bw, (uint32_t)v, cb);
                }
            }
            if (ab) {
                int tc = rot ? rot - 1 : 3;
                for (int s = 0; s < ns; s++) for (int e = 0; e < 2; e++) {
                    int v = val[s][e][tc];
                    if (pk && tc == 0) v >>= 1;
                    put(&bw, (uint32_t)v, ab);
                }
            }
            if (pk == 1) { for (int s = 0; s < ns; s++) for (int e = 0; e < 2; e++) put(&bw, (uint32_t)pb[s][e], 1); }
            else if (pk == 2) { for (int s = 0; s < ns; s++) put(&bw, (uint32_t)pb[s][0], 1); }
            for (int p = 0; p < 16; p++) {
                int sub = sm ? sm[p] : 0;
                int isanc = (p == anc[sub]);
                if (isanc && (pri[p] >> (ib - 1))) st |= ST_ANCHOR;
                put(&bw, (uint32_t)pri[p], isanc ? ib - 1 : ib);
            }
            if (sec) for (int p = 0; p < 16; p++) {
                if (p == 0 && (sec[p] >> (ib2 - 1))) st |= ST_ANCHOR;
                put(&bw, (uint32_t)sec[p], p == 0 ? ib2 - 1 : ib2);
            }
            if (bw.pos != 128) st |= ST_SIGNATURE;
            memcpy(out + (size_t)i * 16, &bw.lo, 8); memcpy(out + (size_t)i * 16 + 8, &bw.hi, 8);
        }
        status[i] = st;
        if (!st) good++;
        if (fields) {
            int16_t *f = fields + (size_t)i * 64;
            f[0] = (int16_t)mode; f[1] = (int16_t)part; f[2] = (int16_t)rot; f[3] = (int16_t)isel;
            for (int s = 0; s < 3; s++) for (int e = 0; e < 2; e++) for (int c = 0; c < 4; c++) f[4 + s * 8 + e * 4 + c] = (int16_t)val[s][e][c];
            for (int p = 0; p < 16; p++) { f[28 + p] = (int16_t)s1[p]; f[44 + p] = (int16_t)s2[p]; }
            f[60] = (int16_t)de; f[61] = (int16_t)di; f[62] = (int16_t)rank; f[63] = 0;
        }
    }
    return good;
}
