/*
 * OpenBH / DREAMNEXTGEN Amlogic DVB audio bridge (PCM first version).
 * Copyright: OpenBH Amlogic integration contributors; GPL-2.0-or-later.
 *
 * DTS/AC3 are DECODED to stereo PCM here, not IEC61937 passthrough.
 * Scope: live DVB / TS-fed audio. Local files are handled by GStreamer.
 * This code never calls the kernel's stub /dev/audiodsp0.
 */
#include <lib/dvb/amlogic_audio.h>
#ifdef DREAMNEXTGEN

#include <alsa/asoundlib.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include <libavutil/samplefmt.h>
#include <libswresample/swresample.h>
}

#include <algorithm>
#include <cstddef>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <fcntl.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <linux/dvb/video.h>

#ifndef VIDEO_GET_PTS
#define VIDEO_GET_PTS _IOR('o', 19, uint64_t)
#endif

namespace {
static void notice(const char *text, int v = 0)
{
    fprintf(stderr, "[OpenBH-AMLAUDIO] %s (%d)\n", text, v);
}
static AVCodecID codecForType(int type)
{
    // Keep in sync with eDVBAudio enum in decoder.h:
    switch (type) {
    case 0: return AV_CODEC_ID_MP2;       // aMPEG
    case 1: return AV_CODEC_ID_AC3;       // aAC3
    case 2: return AV_CODEC_ID_DTS;       // aDTS
    case 3: return AV_CODEC_ID_AAC;       // aAAC
    case 4: return AV_CODEC_ID_AAC_LATM;  // aAACHE / LATM
    case 7: return AV_CODEC_ID_EAC3;      // aDDP
    case 9: return AV_CODEC_ID_AC4;       // aAC4
    default: return AV_CODEC_ID_NONE;
    }
}
static int64_t pesPts(const uint8_t *p)
{
    return ((int64_t)(p[0] & 0x0e) << 29) |
           ((int64_t)p[1] << 22) |
           ((int64_t)(p[2] & 0xfe) << 14) |
           ((int64_t)p[3] << 7) |
           ((int64_t)(p[4] & 0xfe) >> 1);
}

class AudioWorker
{
public:
    AudioWorker(int type, int videoDev, std::atomic<int64_t> &pts)
      : ctx(nullptr), parser(nullptr), swr(nullptr), pcm(nullptr),
        videoFd(-1), lastPts(-1), pidPts(AV_NOPTS_VALUE), outRate(48000),
        ptsRef(pts), decodedFrames(0)
    {
        AVCodecID codec = codecForType(type);
        const AVCodec *implementation = codec != AV_CODEC_ID_NONE ? avcodec_find_decoder(codec) : nullptr;
        if (!implementation) {
            notice("unsupported codec type", type);
            return;
        }
        ctx = avcodec_alloc_context3(implementation);
        if (!ctx || avcodec_open2(ctx, implementation, nullptr) < 0) {
            notice("avcodec_open2 failed", type);
            avcodec_free_context(&ctx);
            return;
        }
        parser = av_parser_init(codec);
        if (!parser) {
            notice("no codec parser", type);
            avcodec_free_context(&ctx);
            return;
        }
        char videoName[64];
        snprintf(videoName, sizeof(videoName), "/dev/dvb/adapter0/video%d", videoDev);
        videoFd = ::open(videoName, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
        const char *selected = getenv("OPENBH_AMLOGIC_ALSA_DEVICE");
        if (!selected || !*selected) selected = "plughw:0,2";
        // Amlogic DreamTwo PCM HDMI is usually TDM-C (card0/dev2).
        int rc = snd_pcm_open(&pcm, selected, SND_PCM_STREAM_PLAYBACK, 0);
        if (rc < 0) {
            fprintf(stderr, "[OpenBH-AMLAUDIO] open %s failed: %s\n", selected, snd_strerror(rc));
            if (!getenv("OPENBH_AMLOGIC_ALSA_DEVICE")) {
                rc = snd_pcm_open(&pcm, "default", SND_PCM_STREAM_PLAYBACK, 0);
            }
        }
        if (rc < 0 || !pcm) {
            notice("ALSA unavailable", rc);
            return;
        }
        rc = snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE,
                                SND_PCM_ACCESS_RW_INTERLEAVED, 2,
                                outRate, 1, 160000);
        if (rc < 0) {
            fprintf(stderr, "[OpenBH-AMLAUDIO] snd_pcm_set_params: %s\n", snd_strerror(rc));
            snd_pcm_close(pcm); pcm = nullptr;
        }
    }
    ~AudioWorker()
    {
        if (pcm) { snd_pcm_drop(pcm); snd_pcm_close(pcm); }
        if (videoFd >= 0) ::close(videoFd);
        swr_free(&swr);
        if (parser) av_parser_close(parser);
        avcodec_free_context(&ctx);
    }
    bool ready() const { return ctx && parser && pcm; }
    void reset()
    {
        if (pcm) { snd_pcm_drop(pcm); snd_pcm_prepare(pcm); }
        if (ctx) avcodec_flush_buffers(ctx);
        if (parser) { // Reset parser state after seek/discontinuity.
            AVCodecID id = ctx->codec_id;
            av_parser_close(parser);
            parser = av_parser_init(id);
        }
        swr_free(&swr);
        lastPts = -1;
        pidPts = AV_NOPTS_VALUE;
    }
    void setPts(int64_t value) { if (value >= 0) pidPts = value; }
    void submit(const uint8_t *data, int size)
    {
        if (!ready() || !data || size <= 0) return;
        // av_parser_parse2 may consume input without output; cap iterations.
        for (int loops = 0; size > 0 && loops < 128; ++loops) {
            uint8_t *out = nullptr;
            int outSize = 0;
            int n = av_parser_parse2(parser, ctx, &out, &outSize,
                       data, size, pidPts, AV_NOPTS_VALUE, 0);
            if (n < 0 || (n == 0 && !outSize)) {
                notice("audio parser stalled", n);
                return;
            }
            data += n; size -= n;
            if (outSize > 0 && out) decode(out, outSize);
            pidPts = AV_NOPTS_VALUE;
        }
    }
private:
    void decode(const uint8_t *buffer, int size)
    {
        AVPacket *pkt = av_packet_alloc();
        if (!pkt) return;
        if (av_new_packet(pkt, size) < 0) { av_packet_free(&pkt); return; }
        memcpy(pkt->data, buffer, size);
        if (lastPts < 0 && parser && parser->pts != AV_NOPTS_VALUE)
            lastPts = parser->pts;
        int rc = avcodec_send_packet(ctx, pkt);
        av_packet_free(&pkt);
        if (rc < 0) return;
        AVFrame *frame = av_frame_alloc();
        if (!frame) return;
        while (avcodec_receive_frame(ctx, frame) == 0) {
            output(frame);
            av_frame_unref(frame);
        }
        av_frame_free(&frame);
    }
    void output(AVFrame *frame)
    {
        if (!frame || frame->nb_samples <= 0 || !pcm) return;
        if (!swr) {
            AVChannelLayout stereo;
            av_channel_layout_default(&stereo, 2);
            int ret = swr_alloc_set_opts2(&swr, &stereo, AV_SAMPLE_FMT_S16, outRate,
                 &frame->ch_layout, (AVSampleFormat)frame->format,
                 frame->sample_rate, 0, nullptr);
            av_channel_layout_uninit(&stereo);
            if (ret < 0 || !swr || swr_init(swr) < 0) {
                notice("FFmpeg resampler configuration failed", ret);
                swr_free(&swr);
                return;
            }
        }
        const int inputRate = frame->sample_rate;
        if (inputRate <= 0) return;
        int capacity = (int)av_rescale_rnd(swr_get_delay(swr, inputRate) + frame->nb_samples,
                                           outRate, inputRate, AV_ROUND_UP);
        if (capacity <= 0 || capacity > 32768) return;
        std::vector<int16_t> pcmData((size_t)capacity * 2);
        uint8_t *dst = reinterpret_cast<uint8_t *>(pcmData.data());
        const uint8_t **src = (const uint8_t **)frame->extended_data;
        int frames = swr_convert(swr, &dst, capacity, src, frame->nb_samples);
        if (frames <= 0) return;

        // Best-effort A/V alignment. This is not a replacement for OpenATV's
        // full clock/avsync_core implementation. Nonblocking video PTS ioctl.
        uint64_t videoPts = 0;
        if (videoFd >= 0 && lastPts >= 0 &&
            ::ioctl(videoFd, VIDEO_GET_PTS, &videoPts) == 0 && videoPts) {
            int64_t delta = lastPts - (int64_t)videoPts;
            if (delta < -45000) { // audio >500ms late: discard stale PCM
                lastPts += (int64_t)frame->nb_samples * 90000 / inputRate;
                return;
            }
            if (delta > 9000 && delta < 45000) // audio slightly ahead
                usleep((useconds_t)std::min((int64_t)80000, delta * 1000000 / 90000));
        }
        int offset = 0;
        while (offset < frames) {
            snd_pcm_sframes_t n = snd_pcm_writei(pcm, pcmData.data() + offset * 2,
                                                  (snd_pcm_uframes_t)(frames - offset));
            if (n == -EPIPE || n == -ESTRPIPE) {
                snd_pcm_prepare(pcm);
                continue;
            }
            if (n < 0) {
                fprintf(stderr, "[OpenBH-AMLAUDIO] ALSA write: %s\n", snd_strerror((int)n));
                break;
            }
            if (n == 0) break;
            offset += (int)n;
        }
        ++decodedFrames;
        if (lastPts >= 0) {
            lastPts += (int64_t)frame->nb_samples * 90000 / inputRate;
            ptsRef.store(lastPts);
        }
        if (decodedFrames == 1) notice("first decoded audio frame output to ALSA", frames);
    }
    AVCodecContext *ctx;
    AVCodecParserContext *parser;
    SwrContext *swr;
    snd_pcm_t *pcm;
    int videoFd;
    int64_t lastPts, pidPts;
    unsigned int outRate;
    std::atomic<int64_t> &ptsRef;
    unsigned int decodedFrames;
};
}

OpenBHAmlogicAudio::OpenBHAmlogicAudio(int demuxFd, int videoDev)
    : m_demuxFd(demuxFd), m_videoDev(videoDev), m_pid(-1), m_type(-1),
      m_thread(), m_started(false), m_stop(true), m_paused(false),
      m_flush(false), m_pts(-1) {}
OpenBHAmlogicAudio::~OpenBHAmlogicAudio() { stop(); }
int OpenBHAmlogicAudio::start(int audioPid, int enigmaAudioType)
{
    stop();
    if (m_demuxFd < 0) return -ENODEV;
    if (codecForType(enigmaAudioType) == AV_CODEC_ID_NONE) {
        notice("unsupported DreamNextGen audio format", enigmaAudioType);
        return -ENOTSUP;
    }
    m_pid = audioPid;
    m_type = enigmaAudioType;
    m_pts.store(-1);
    m_paused.store(false);
    m_flush.store(false);
    m_stop.store(false);
    int rc = pthread_create(&m_thread, nullptr, runThread, this);
    if (rc != 0) { m_stop.store(true); return -rc; }
    m_started = true;
    return 0;
}
void OpenBHAmlogicAudio::stop()
{
    if (!m_started) return;
    m_stop.store(true);
    pthread_join(m_thread, nullptr);
    m_started = false;
}
void OpenBHAmlogicAudio::pause(bool paused) { m_paused.store(paused); }
void OpenBHAmlogicAudio::flush() { m_flush.store(true); }
void *OpenBHAmlogicAudio::runThread(void *arg)
{
    static_cast<OpenBHAmlogicAudio *>(arg)->run();
    return nullptr;
}
void OpenBHAmlogicAudio::run()
{
    AudioWorker worker(m_type, m_videoDev, m_pts);
    if (!worker.ready()) { notice("ALSA/FFmpeg worker init failed"); return; }
    // TSDEMUX_TAP returns multiplexed TS, not elementary-stream PCM.
    std::vector<uint8_t> input;
    input.reserve(64 * 1024);
    bool havePes = false;
    uint8_t lastCC = 0;
    bool haveCC = false;
    uint8_t buf[188 * 128];
    while (!m_stop.load()) {
        if (m_flush.exchange(false)) {
            worker.reset();
            input.clear();
            havePes = false;
            haveCC = false;
        }
        if (m_paused.load()) { usleep(20000); continue; }
        pollfd pfd = {m_demuxFd, POLLIN | POLLERR | POLLHUP, 0};
        int r = ::poll(&pfd, 1, 100);
        if (r <= 0 || !(pfd.revents & POLLIN)) continue;
        ssize_t got = ::read(m_demuxFd, buf, sizeof(buf));
        if (got <= 0) continue;
        input.insert(input.end(), buf, buf + got);
        size_t pos = 0;
        while (input.size() - pos >= 188) {
            if (input[pos] != 0x47) { ++pos; continue; }
            // Require another sync before consuming a packet when possible.
            if (input.size() - pos >= 376 && input[pos + 188] != 0x47) { ++pos; continue; }
            const uint8_t *ts = input.data() + pos;
            pos += 188;
            int pid = ((ts[1] & 0x1f) << 8) | ts[2];
            if (pid != m_pid || (ts[1] & 0x80)) continue;
            int afc = (ts[3] >> 4) & 3;
            if (afc != 1 && afc != 3) continue;
            uint8_t cc = ts[3] & 0x0f;
            bool pusi = (ts[1] & 0x40) != 0;
            if (haveCC && cc != (uint8_t)((lastCC + 1) & 15)) {
                havePes = false; worker.reset();
            }
            lastCC = cc; haveCC = true;
            int offset = 4;
            if (afc == 3) offset += 1 + ts[4];
            if (offset >= 188) continue;
            if (pusi) {
                havePes = false;
                if (188 - offset < 9 || memcmp(ts + offset, "\x00\x00\x01", 3) != 0) continue;
                uint8_t streamID = ts[offset + 3];
                if (streamID != 0xbd && (streamID < 0xc0 || streamID > 0xdf)) continue;
                int headerLen = 9 + ts[offset + 8];
                if (offset + headerLen > 188) continue;
                if ((ts[offset + 7] & 0x80) && ts[offset + 8] >= 5)
                    worker.setPts(pesPts(ts + offset + 9));
                offset += headerLen;
                havePes = true;
            }
            if (havePes && offset < 188)
                worker.submit(ts + offset, 188 - offset);
        }
        if (pos) input.erase(input.begin(), input.begin() + (std::ptrdiff_t)pos);
        if (input.size() > 256 * 1024) { input.clear(); havePes = false; }
    }
}
#endif // DREAMNEXTGEN
