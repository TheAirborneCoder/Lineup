/* BCPack lane array decoder (clean-room, see bcpack.py for the format description).
   Build (MSVC):  cl /nologo /O2 /LD bcpack_native.c /Fe:bcpack_native.dll
   Build (gcc):   gcc -O2 -shared -o bcpack_native.so bcpack_native.c            */
#include <stdint.h>
#include <string.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

#define L (1u << 24)
#define MAXL 32
#define MAXW 10
#define BUDGET 8
#define VZMAX 8
enum { HDR0, HDR1, HDR2, PROC };
enum { OK = 0, E_TABLE = 1, E_END = 2, E_INPUT = 3, E_SYMBOL = 4, E_WIDTH = 5, E_LANES = 6 };

typedef struct { int w; int32_t freq[MAXW + 1], base[MAXW + 1]; } Model;
typedef struct {
    int wdef, ctrl, out_rem, w, q, h3, flush, done, full, held, vz, inbuf, sym, in_rem, nout;
    uint32_t p0, p1; const Model *m;
} Lane;

static void build(Model *m, int w, const int32_t *fq) {
    int acc = 0; m->w = w;
    for (int p = 0; p <= w; p++) { m->freq[p] = fq[p]; m->base[p] = acc; acc += fq[p] * (p == 0 ? 1 : (1 << (p - 1))); }
}
static int lookup(const Model *m, int cf) {
    int p = m->w;
    while (p > 0 && cf < m->base[p]) p--;
    return p == 0 ? 0 : (1 << (p - 1)) + (cf - m->base[p]) / m->freq[p];
}
static uint32_t advance(const Model *m, int s, uint32_t x) {
    int p = 0, t = s;
    while (t) { p++; t >>= 1; }
    uint32_t f = (uint32_t)m->freq[p], c = (uint32_t)m->base[p] + f * (uint32_t)(s - (p == 0 ? 0 : (1 << (p - 1))));
    if (c == 0) { f -= 1; c = 1; }                      /* symbol 0 owns [1, f0) */
    return f * (x >> 16) + (x & 0xFFFF) - c;
}

/* tables: int32[(MAXW+1) * 16 * (MAXW+1)] indexed [w][q][p]; known: uint8[(MAXW+1) * 16].
   out: uint16[nl * nsym], lane-major. info: [0] error, [1] input position, [2] lane, [3] w, [4] q. */
EXPORT int bcpack_lanes(const uint8_t *raw, int nraw, int nl, const int *wdef, int nsym,
                        const int32_t *tables, const uint8_t *known, uint16_t *out, int32_t *info)
{
    static Model models[MAXW + 1][16];
    uint8_t built[MAXW + 1][16];
    Lane ln[MAXL];
    int slotfull[MAXL], slotval[MAXL], stall[MAXL], pos = 0;
    if (nl < 1 || nl > MAXL) { info[0] = E_LANES; return E_LANES; }
    memset(built, 0, sizeof(built)); memset(ln, 0, sizeof(ln)); memset(slotfull, 0, sizeof(slotfull));
    for (int i = 0; i < nl; i++) { ln[i].wdef = wdef[i]; ln[i].out_rem = nsym; if (nsym <= 0) ln[i].done = 1; }
#define FAIL(code, lane) do { info[0] = code; info[1] = pos; info[2] = lane; info[3] = ln[lane].w; info[4] = ln[lane].q; return code; } while (0)
    for (;;) {
        int alldone = 1;
        for (int i = 0; i < nl; i++) if (!ln[i].done || slotfull[i]) { alldone = 0; break; }
        if (alldone) { info[0] = OK; info[1] = pos; return OK; }
        int left = BUDGET;
        for (int i = 0; i < nl; i++) {
            Lane *d = &ln[i];
            if (d->done) { stall[i] = 1; continue; }
            stall[i] = d->held && slotfull[i];                       /* output not taken yet */
            if (stall[i]) continue;
            if (d->ctrl == PROC) d->p0 = d->held ? advance(d->m, d->sym, d->p1) : d->p1;
            if (!d->full && (d->ctrl != PROC || d->in_rem > 0)) {     /* input buffer wants a byte */
                if (left == 0) { stall[i] = 1; continue; }           /* not served: the lane freezes */
                if (pos >= nraw) FAIL(E_INPUT, i);
                d->inbuf = raw[pos++]; d->full = 1; left--;
            }
            if (d->held) { slotfull[i] = 1; slotval[i] = d->sym; d->held = 0; }
        }
        for (int i = 0; i < nl; i++) {
            Lane *d = &ln[i];
            if (d->done || stall[i]) continue;
            if (d->ctrl == HDR0) {
                if (!d->full) continue;
                int h = d->inbuf;
                d->q = h & 15; d->flush = (h >> 4) & 1; d->h3 = (h >> 5) & 1; d->w = d->wdef - (h >> 6);
                d->full = 0; d->ctrl = HDR1; d->vz = 0;
            } else if (d->ctrl == HDR1) {
                if (!d->full) continue;
                d->full = 0;
                if (d->w < 1 || d->w > MAXW) FAIL(E_WIDTH, i);
                if (!known[d->w * 16 + d->q]) FAIL(E_TABLE, i);
                if (!built[d->w][d->q]) { build(&models[d->w][d->q], d->w, &tables[(d->w * 16 + d->q) * (MAXW + 1)]); built[d->w][d->q] = 1; }
                d->m = &models[d->w][d->q];
                if (d->h3) { d->in_rem = d->inbuf; d->ctrl = HDR2; } else { d->in_rem = d->inbuf + 1; d->ctrl = PROC; }
            } else if (d->ctrl == HDR2) {
                if (!d->full) continue;
                d->in_rem = ((d->inbuf << 8) | (d->in_rem & 0xFF)) + 257; d->ctrl = PROC; d->full = 0;
            } else {
                uint32_t st = d->p0;
                if (d->p0 < L) {
                    if (d->in_rem > 0) {
                        if (d->full) {
                            st = (d->p0 << 8) | (uint32_t)d->inbuf; d->in_rem--; d->full = 0;
                            if (d->in_rem == 0 && d->flush) while (st < L && st != 0 && d->vz < VZMAX) { st <<= 8; d->vz++; }
                        }
                    } else if (d->flush) {                             /* trimmed zero bytes */
                        if (d->p0 == 0 || d->vz >= VZMAX) FAIL(E_END, i);
                        while (st < L && st != 0 && d->vz < VZMAX) { st <<= 8; d->vz++; }
                    } else {                                           /* table switch, state carried */
                        d->p1 = st;
                        if (d->out_rem > 0) d->ctrl = HDR0; else d->done = 1;
                        continue;
                    }
                }
                int nxt = st > L && d->out_rem > 0;
                if (nxt) { d->sym = lookup(d->m, st & 0xFFFF); d->out_rem--; if (d->sym >> d->w) FAIL(E_SYMBOL, i); }
                d->held = nxt; d->p1 = st;
                if (d->in_rem == 0) {
                    if (d->flush) {
                        if (st == L) { if (d->out_rem > 0) { d->ctrl = HDR0; d->p0 = 0; d->p1 = 0; } else d->done = 1; }
                        else if (st > L && !nxt) FAIL(E_END, i);
                    } else if (d->out_rem == 0 && !nxt) d->done = 1;
                } else if (st == L || (d->out_rem == 0 && !nxt && st > L)) FAIL(E_END, i);
            }
        }
        int ready = 1, any = 0;
        for (int i = 0; i < nl; i++) {
            if (slotfull[i]) any = 1;
            else if (!(ln[i].done && !ln[i].held)) { ready = 0; break; }
        }
        if (ready && any) {
            for (int i = 0; i < nl; i++) if (slotfull[i]) {
                if (ln[i].nout < nsym) out[(size_t)i * nsym + ln[i].nout] = (uint16_t)slotval[i];
                ln[i].nout++; slotfull[i] = 0;
            }
        }
        for (int i = 0; i < nl; i++) if (ln[i].done && ln[i].held && !slotfull[i]) { slotfull[i] = 1; slotval[i] = ln[i].sym; ln[i].held = 0; }
    }
}
