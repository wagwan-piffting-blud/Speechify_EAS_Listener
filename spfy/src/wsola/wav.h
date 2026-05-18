#ifndef SPFY_WSOLA_WAV_H
#define SPFY_WSOLA_WAV_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

/* Streaming s16 PCM sink used by the WSOLA streamer.
 *
 * Two modes:
 *   - File: spfy_wav_open()  — writes a minimal RIFF/WAVE file (CLI path).
 *   - Callback: spfy_wav_open_callback() — invokes the caller's write
 *     function for each chunk. No header is emitted; the caller decides
 *     framing. Used by spfy_sapi.dll to stream PCM straight to the SAPI
 *     ISpTTSEngineSite without buffering an intermediate file.
 *
 * Either way, spfy_wav_write() is the single mid-stream entry point and
 * WSOLA's call sites are mode-agnostic. */

/* Callback signature for streaming sinks. Return SPFY_OK to continue or
 * any SPFY_E_* to abort the synth. `samples` is a chunk of int16 mono
 * PCM at the writer's `sample_rate`; chunk sizes are not bounded
 * (WSOLA emits OLA-mix bursts of 80 samples and unit bodies of up to a
 * few thousand). */
typedef int (*spfy_wav_write_fn)(void *ctx, const int16_t *samples,
                                 size_t n_samples);

typedef struct {
    /* Mutually exclusive: fp set for file mode, write_cb set for
     * callback mode. (Exactly one is non-NULL between open and close.) */
    FILE             *fp;
    spfy_wav_write_fn write_cb;
    void             *cb_ctx;
    uint32_t          sample_rate;
    uint32_t          n_samples_written;   /* tracked for header back-fill */
} spfy_wav_writer_t;

int  spfy_wav_open (spfy_wav_writer_t *w, const char *path,
                    uint32_t sample_rate);
int  spfy_wav_open_callback(spfy_wav_writer_t *w,
                            spfy_wav_write_fn cb, void *ctx,
                            uint32_t sample_rate);
int  spfy_wav_write(spfy_wav_writer_t *w, const int16_t *samples, size_t n);
int  spfy_wav_close(spfy_wav_writer_t *w);

#endif
