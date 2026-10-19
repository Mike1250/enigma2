#ifndef __DREAMNEXTGEN_OPENATV_PCM_BACKEND_H
#define __DREAMNEXTGEN_OPENATV_PCM_BACKEND_H

/* PCM fallback for the real OpenATV TS/PES parser. This file is new code,
 * NOT a copy of OpenATV's unavailable ALSA/AV-sync backend. */
#include <atomic>
#include <stdint.h>
#include <alsa/asoundlib.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include <libswresample/swresample.h>
}
#include <lib/dvb/idvb.h>

extern std::atomic<int64_t> g_dreamnextgen_audio_presented_pts;

class eAVSyncCore
{
public:
    static int64_t videoPTS90k();
    static int64_t delta90k(int64_t lhs, int64_t rhs);
};

class eAlsaOutput
{
public:
    eAlsaOutput();
    ~eAlsaOutput();
    int start(unsigned int rate);
    void stop();
    void flush();
    void pause(bool p);
    int pushData(const int16_t *pcm, unsigned int frames, int64_t pts90k);
    int getPTS(pts_t &pts) const;
private:
    snd_pcm_t *m_pcm;
    unsigned int m_rate;
    std::atomic<int64_t> m_presentedPTS;
    int64_t m_nextPTS;
    std::atomic<bool> m_paused;
};

class eAudioDecoder
{
public:
    eAudioDecoder();
    ~eAudioDecoder();
    int start(int rate, int channels, int sample_size, enum AVCodecID codec);
    int decode(uint8_t *data, int size, int64_t pts, int64_t dts);
    int getPTS(pts_t &pts) const;
    void pause(bool p);
    void flush();
    eAlsaOutput *m_AlsaOutput;
private:
    const AVCodec *m_codec;
    AVCodecContext *m_ctx;
    AVFrame *m_frame;
    AVPacket *m_packet;
    SwrContext *m_swr;
    int m_swrRate;
    int m_rate;
    int64_t m_nextPTS;
};
#endif
