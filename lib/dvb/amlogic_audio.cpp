/* DreamNextGen audio backend for OpenATV's actual TS/PES parser.
 * The eTsParser implementation is imported from the user's OpenATV source
 * comparison. ALSA/AVSync code below is an explicitly new PCM-only backend.
 * Do not confuse it with OpenATV's unavailable proprietary backend pieces.
 *
 * The existing OpenBH build already compiles amlogic_audio.cpp. Include the
 * companion parser translation unit once to avoid changing unknown OE build
 * system revisions (some use Makefile.am, others Makefile.inc). Do not add
 * tsparser.cpp as a separate object unless this include is removed.
 */
#include <lib/dvb/openatv_audio_backend.h>
#include <lib/base/eerror.h>
#include <lib/dvb/volume.h>
#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <unistd.h>

std::atomic<int64_t> g_dreamnextgen_audio_presented_pts(-1);

int64_t eAVSyncCore::videoPTS90k()
{
    /* Clock belongs to the *current displayed decoder*, not the demux's
       first packet, so check the kernel presentation clock. */
    FILE *f = fopen("/sys/class/tsync/pts_video", "r");
    if (!f) return -1;
    unsigned long long v = 0;
    int ok = fscanf(f, "%llx", &v);
    fclose(f);
    if (ok != 1 || !v) return -1;
    return (int64_t)(v & ((1ULL << 33) - 1));
}
int64_t eAVSyncCore::delta90k(int64_t lhs, int64_t rhs)
{
    const int64_t mask = (1LL << 33) - 1;
    int64_t d = (lhs - rhs) & mask;
    if (d >= (1LL << 32)) d -= (1LL << 33);
    return d;
}

eAlsaOutput::eAlsaOutput(): m_pcm(nullptr), m_rate(0), m_presentedPTS(-1),
    m_nextPTS(-1), m_paused(false) {}
eAlsaOutput::~eAlsaOutput() { stop(); }
int eAlsaOutput::start(unsigned int rate)
{
    stop();
    if (!rate || rate > 192000) return -EINVAL;
    const char *devices[] = {"hw:0,0", "default"};
    for (const char *dev : devices) {
        if (snd_pcm_open(&m_pcm, dev, SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK) == 0)
            break;
    }
    if (!m_pcm) return -ENODEV;
    int err = snd_pcm_set_params(m_pcm, SND_PCM_FORMAT_S16_LE,
        SND_PCM_ACCESS_RW_INTERLEAVED, 2, rate, 1, 320000);
    if (err < 0) {
        eWarning("[OpenATV-PCM] ALSA configuration failed: %s", snd_strerror(err));
        stop();
        return err;
    }
    m_rate = rate;
    m_nextPTS = -1;
    m_presentedPTS.store(-1);
    g_dreamnextgen_audio_presented_pts.store(-1);
    return 0;
}
void eAlsaOutput::stop()
{
    if (m_pcm) { snd_pcm_drop(m_pcm); snd_pcm_close(m_pcm); m_pcm = nullptr; }
    m_rate = 0;
    m_nextPTS = -1;
    m_presentedPTS.store(-1);
    g_dreamnextgen_audio_presented_pts.store(-1);
}
void eAlsaOutput::flush()
{
    if (m_pcm) { snd_pcm_drop(m_pcm); snd_pcm_prepare(m_pcm); }
    m_nextPTS = -1;
    m_presentedPTS.store(-1);
    g_dreamnextgen_audio_presented_pts.store(-1);
}
/* Enigma2 native V4: preserve successful unpause; recover failed ALSA resumes. */
static int e2_native_v4_pause(snd_pcm_t *pcm, int pause_requested)
{
    int ret = snd_pcm_pause(pcm, pause_requested);
    if (ret >= 0 || pause_requested)
        return ret;
    ret = snd_pcm_recover(pcm, ret, 1);
    if (ret >= 0)
        return ret;
    snd_pcm_drop(pcm);
    return snd_pcm_prepare(pcm);
}

void eAlsaOutput::pause(bool p)
{
    m_paused.store(p);
    if (!m_pcm) return;
    if (e2_native_v4_pause(m_pcm, p ? 1 : 0) < 0 && p) {
        /* Unsupported pause: do not flush or invalidate the timeline. */
        eWarning("[OpenATV-PCM] ALSA hardware pause unavailable");
    }
}
int eAlsaOutput::pushData(const int16_t *pcm, unsigned int frames, int64_t pts90k)
{
	unsigned int e2_v4_eagain_retries = 0;
    if (!m_pcm || !m_rate || !pcm) return -ENODEV;
    if (pts90k >= 0) {
        pts90k &= ((1LL << 33)-1);
        if (m_nextPTS < 0 ||
            llabs(eAVSyncCore::delta90k(pts90k, m_nextPTS)) > 9000)
            m_nextPTS = pts90k; /* new PES/seek/codec discontinuity */
    }
    if (m_nextPTS < 0) return 0; /* don't invent an APTS */

    /* Compare the actual ALSA *presentation* PTS, not the end-of-write PTS.
       Use only small bounded corrections when PTS domains are comparable. */
    int64_t video = eAVSyncCore::videoPTS90k();
    snd_pcm_sframes_t queued = 0;
    if (snd_pcm_state(m_pcm) == SND_PCM_STATE_RUNNING &&
        snd_pcm_delay(m_pcm, &queued) < 0) queued = 0;
    if (queued < 0 || queued > (snd_pcm_sframes_t)(3 * m_rate)) queued = 0;
    if (video >= 0) {
        int64_t audioPresent = (m_nextPTS - queued * 90000 / m_rate) & ((1LL << 33)-1);
        int64_t err = eAVSyncCore::delta90k(audioPresent, video);
        if (llabs(err) < 5 * 90000) { /* only same PTS epoch */
            if (err < -9000) {
                unsigned int drop = (unsigned int)std::min<int64_t>(frames,(-err - 4500)*m_rate/90000);
                pcm += 2 * drop;
                frames -= drop;
                m_nextPTS += (int64_t)drop * 90000 / m_rate;
            } else if (err > 9000) {
                /* Audio ahead: wait briefly. No fixed global audio offset. */
                unsigned int hold = (unsigned int)std::min<int64_t>(12000, ((err - 4500)*1000000/90000));
                if (hold) usleep(hold);
            }
        }
    }
    /* Honor Enigma2's volume/mute state even on raw ALSA hw PCM, which
     * does not go through the /dev/dvb/audio volume ioctl. */
    std::vector<int16_t> adjusted;
    eDVBVolumecontrol *vc = eDVBVolumecontrol::getInstance();
    if (vc) {
        int volume = vc->isMuted() ? 0 : std::max(0,std::min(100,vc->getVolume()));
        if (volume < 100 && frames) {
            adjusted.assign(pcm,pcm+2*frames);
            for (size_t i=0; i<adjusted.size(); ++i)
                adjusted[i] = (int16_t)((int32_t)adjusted[i]*volume/100);
            pcm = adjusted.data();
        }
    }
    unsigned int remaining = frames;
    unsigned int tries = 0;
    while (remaining && !m_paused.load() && tries++ < 500) {
        snd_pcm_sframes_t n = snd_pcm_writei(m_pcm, pcm, remaining);
        if (n == -EAGAIN) { if (++e2_v4_eagain_retries == 225 && snd_pcm_state(m_pcm) == SND_PCM_STATE_RUNNING)
		{
			/* Restart only a RUNNING PCM that has failed writes for ~450 ms. */
			flush();
		}
		usleep(2000); continue; }
        if (n < 0) {
            if (snd_pcm_recover(m_pcm, (int)n, 1) < 0) return (int)n;
            continue;
        }
        if (!n) break;
        pcm += 2 * n;
        remaining -= (unsigned int)n;
        m_nextPTS += (int64_t)n * 90000 / m_rate;
    }
    if (snd_pcm_state(m_pcm) == SND_PCM_STATE_RUNNING &&
        snd_pcm_delay(m_pcm, &queued) == 0 && queued >= 0 &&
        queued < (snd_pcm_sframes_t)(3*m_rate)) {
        m_presentedPTS.store((m_nextPTS - queued * 90000 / m_rate)&((1LL<<33)-1));
        g_dreamnextgen_audio_presented_pts.store(m_presentedPTS.load());
    } else {
        m_presentedPTS.store(-1);
        g_dreamnextgen_audio_presented_pts.store(-1);
    }
    return (int)(frames-remaining);
}
int eAlsaOutput::getPTS(pts_t &pts) const
{
    int64_t value = m_presentedPTS.load();
    if (value < 0) return -1;
    pts = (pts_t)value;
    return 0;
}

eAudioDecoder::eAudioDecoder(): m_AlsaOutput(new eAlsaOutput()),
    m_codec(nullptr), m_ctx(nullptr), m_frame(av_frame_alloc()),
    m_packet(av_packet_alloc()), m_swr(nullptr), m_swrRate(0), m_rate(0), m_nextPTS(-1) {}
eAudioDecoder::~eAudioDecoder()
{
    if (m_AlsaOutput) { delete m_AlsaOutput; m_AlsaOutput = nullptr; }
    if (m_swr) swr_free(&m_swr);
    if (m_ctx) avcodec_free_context(&m_ctx);
    if (m_frame) av_frame_free(&m_frame);
    if (m_packet) av_packet_free(&m_packet);
}
int eAudioDecoder::start(int rate, int channels, int sample_size, enum AVCodecID codec)
{
    (void)channels; (void)sample_size;
    m_codec = avcodec_find_decoder(codec);
    if (!m_codec) { eWarning("[OpenATV-PCM] missing codec %d", codec); return -ENOTSUP; }
    m_ctx = avcodec_alloc_context3(m_codec);
    if (!m_ctx || avcodec_open2(m_ctx, m_codec, nullptr) < 0) return -EINVAL;
    m_rate = rate ? rate : 48000;
    return 0;
}
int eAudioDecoder::decode(uint8_t *data, int size, int64_t pts, int64_t dts)
{
    (void)dts;
    if (!m_ctx || !m_packet || size <= 0) return -EINVAL;
    m_packet->data = data;
    m_packet->size = size;
    m_packet->pts = pts;
    int sent = avcodec_send_packet(m_ctx, m_packet);
    m_packet->data = nullptr; m_packet->size = 0;
    if (sent < 0 && sent != AVERROR(EAGAIN)) return sent;
    while (avcodec_receive_frame(m_ctx,m_frame) == 0) {
        int rate = m_frame->sample_rate ? m_frame->sample_rate : m_rate;
        if (!m_swr || rate != m_swrRate) {
            if (m_swr) swr_free(&m_swr);
            AVChannelLayout stereo = AV_CHANNEL_LAYOUT_STEREO;
            if (swr_alloc_set_opts2(&m_swr,&stereo,AV_SAMPLE_FMT_S16,rate,
                &m_frame->ch_layout,(enum AVSampleFormat)m_frame->format,rate,0,nullptr)<0 ||
                !m_swr || swr_init(m_swr)<0) return -EINVAL;
            m_swrRate = rate;
            if (m_AlsaOutput->start(rate) < 0) return -ENODEV;
        }
        int outframes = (int)av_rescale_rnd(swr_get_delay(m_swr,rate) +
            m_frame->nb_samples,rate,rate,AV_ROUND_UP);
        if (outframes <= 0) { av_frame_unref(m_frame); continue; }
        std::vector<int16_t> out((size_t)outframes*2);
        uint8_t *dst[] = {(uint8_t*)out.data()};
        int wrote = swr_convert(m_swr,dst,outframes,
            (const uint8_t**)m_frame->extended_data,m_frame->nb_samples);
        if (wrote > 0) {
            if (m_nextPTS < 0 || (pts >= 0 &&
                llabs(eAVSyncCore::delta90k(pts,m_nextPTS)) > 9000))
                m_nextPTS = pts;
            if (m_nextPTS >= 0) {
                m_AlsaOutput->pushData(out.data(),(unsigned int)wrote,m_nextPTS);
                m_nextPTS += (int64_t)wrote * 90000 / rate;
            }
        }
        av_frame_unref(m_frame);
    }
    return 0;
}
int eAudioDecoder::getPTS(pts_t &pts) const { return m_AlsaOutput ? m_AlsaOutput->getPTS(pts) : -1; }
void eAudioDecoder::pause(bool p) { if(m_AlsaOutput) m_AlsaOutput->pause(p); }
void eAudioDecoder::flush()
{
    if (m_ctx) avcodec_flush_buffers(m_ctx);
    if (m_AlsaOutput) m_AlsaOutput->flush();
    m_nextPTS = -1;
}

/* This is compiled once by the existing amlogic_audio.cpp build target. */
#include <lib/dvb/tsparser.cpp>
