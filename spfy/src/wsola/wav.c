#include "wav.h"
#include "../../include/spfy/spfy.h"

#include <stdlib.h>
#include <string.h>

static void put_le_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v       & 0xFF);
    p[1] = (uint8_t)((v >> 8 )& 0xFF);
    p[2] = (uint8_t)((v >> 16)& 0xFF);
    p[3] = (uint8_t)((v >> 24)& 0xFF);
}

static void put_le_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v       & 0xFF);
    p[1] = (uint8_t)((v >> 8)& 0xFF);
}

static int write_header(spfy_wav_writer_t *w, uint32_t n_samples)
{
    uint8_t hdr[44];
    uint32_t data_size = n_samples * 2u;            /* s16 = 2 bytes */
    uint32_t riff_size = data_size + 36u;            /* 44 - 8 */
    memcpy(hdr + 0,  "RIFF", 4);
    put_le_u32(hdr + 4, riff_size);
    memcpy(hdr + 8,  "WAVE", 4);
    memcpy(hdr + 12, "fmt ", 4);
    put_le_u32(hdr + 16, 16u);                       /* fmt size */
    put_le_u16(hdr + 20, 1u);                        /* PCM */
    put_le_u16(hdr + 22, 1u);                        /* channels */
    put_le_u32(hdr + 24, w->sample_rate);
    put_le_u32(hdr + 28, w->sample_rate * 2u);       /* byte rate */
    put_le_u16(hdr + 32, 2u);                        /* block align */
    put_le_u16(hdr + 34, 16u);                       /* bits */
    memcpy(hdr + 36, "data", 4);
    put_le_u32(hdr + 40, data_size);
    if (fseek(w->fp, 0, SEEK_SET) != 0) return SPFY_E_IO;
    if (fwrite(hdr, 1, 44, w->fp) != 44u) return SPFY_E_IO;
    return SPFY_OK;
}

int spfy_wav_open(spfy_wav_writer_t *w, const char *path, uint32_t sample_rate)
{
    if (!w || !path) return SPFY_E_INVAL;
    memset(w, 0, sizeof *w);
    w->fp = fopen(path, "wb+");
    if (!w->fp) return SPFY_E_IO;
    w->sample_rate = sample_rate;
    /* Reserve header space; we'll back-fill on close. */
    return write_header(w, 0);
}

int spfy_wav_open_callback(spfy_wav_writer_t *w, spfy_wav_write_fn cb,
                           void *ctx, uint32_t sample_rate)
{
    if (!w || !cb) return SPFY_E_INVAL;
    memset(w, 0, sizeof *w);
    w->write_cb    = cb;
    w->cb_ctx      = ctx;
    w->sample_rate = sample_rate;
    return SPFY_OK;
}

int spfy_wav_write(spfy_wav_writer_t *w, const int16_t *samples, size_t n)
{
    if (!w) return SPFY_E_INVAL;
    if (n == 0) return SPFY_OK;
    if (w->fp) {
        if (fwrite(samples, sizeof *samples, n, w->fp) != n)
            return SPFY_E_IO;
    } else if (w->write_cb) {
        int rc = w->write_cb(w->cb_ctx, samples, n);
        if (rc != SPFY_OK) return rc;
    } else {
        return SPFY_E_INVAL;
    }
    w->n_samples_written += (uint32_t)n;
    return SPFY_OK;
}

int spfy_wav_close(spfy_wav_writer_t *w)
{
    if (!w) return SPFY_E_INVAL;
    if (w->fp) {
        int rc = write_header(w, w->n_samples_written);
        if (fclose(w->fp) != 0 && rc == SPFY_OK) rc = SPFY_E_IO;
        w->fp = NULL;
        return rc;
    }
    /* Callback mode: nothing to finalize. */
    w->write_cb = NULL;
    w->cb_ctx   = NULL;
    return SPFY_OK;
}
