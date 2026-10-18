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
#include <lib/dvb/volume.h>

#include <alsa/asoundlib.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include <libavutil/samplefmt.h>
#include <libswresample/swresample.h>
}

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cerrno>
#include <cstdio>
#include <cstdarg>
#include <ctime>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <fcntl.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <linux/dvb/video.h>

// Read-only sysfs hardware clock. Never open video0 here: one implementation
// resets the decoder when an additional video handle is opened.
#define E2_OBH_LIVE_CLOCK_SYSFS "/sys/module/aml_hardware_dmx/parameters/e2_obh_live_clock_vpts"

namespace {
// Small, bounded diagnostic log kept on the receiver's /tmp filesystem.
// The normal OpenBH/OpenATV/Pure2 driver paths are not involved.
static void audioLog(const char *format, ...)
{
    char message[640];
    va_list args;
    va_start(args, format);
    vsnprintf(message, sizeof(message), format, args);
    va_end(args);
    fprintf(stderr, "[OpenBH-AMLAUDIO-MIN-SYNC] %s\n", message);
    FILE *log = fopen("/tmp/openbh_amlogic_audio_minsync.log", "a");
    if (log) {
        time_t now = time(nullptr);
        fprintf(log, "%lld %s\n", (long long)now, message);
        fclose(log);
    }
}
static void notice(const char *text, int v = 0)
{
    audioLog("%s (%d)", text, v);
}
static bool enableVideoSync()
{
    // Enable by default: V3's unrestricted PCM could run ahead of the video.
    // Disable for an A/B comparison with OPENBH_AMLOGIC_AVSYNC=0.
    const char *value = getenv("OPENBH_AMLOGIC_AVSYNC");
    return !value || strcmp(value, "0") != 0;
}
static bool enableSoftwareVolume()
{
    const char *value = getenv("OPENBH_AMLOGIC_SOFTWARE_VOLUME");
    return !value || strcmp(value, "0") != 0;
}
// PTS is a 33-bit 90-kHz counter. Preserve correct differences over wrap.
static int64_t ptsDelta(int64_t a, int64_t b)
{
    constexpr int64_t wrap = (1LL << 33);
    constexpr int64_t half = wrap / 2;
    int64_t d = (a - b) & (wrap - 1);
    return (d >= half) ? (d - wrap) : d;
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
    AudioWorker(int type, int videoDev, std::atomic<int64_t> &pts,
                std::atomic<bool> &stopRequested)
      : ctx(nullptr), parser(nullptr), swr(nullptr), pcm(nullptr),
        videoFd(-1), lastPts(-1), pidPts(AV_NOPTS_VALUE), outRate(48000),
        ptsRef(pts), stopFlag(stopRequested), decodedFrames(0), pcmFramesWritten(0),
        xruns(0), writeErrors(0), ptsDrops(0), syncWaits(0), syncWaitMs(0),
        initialSyncDone(false), startupSilenceMs(0), startupSkipMs(0),
        lastAvDeltaMs(0), lastSeenParserPts(AV_NOPTS_VALUE),
        clockUnsupported(false), vptsLast(0), clockReads(0),
        clockReady(0), autoUpdates(0), lastCompensation(0),
        startupDiscarded(0), filteredError(0.0),
        syncEnabled(enableVideoSync()), softwareVolume(enableSoftwareVolume()),
        lastAppliedVolume(-1), lastAppliedMute(false),
        createdAt(std::chrono::steady_clock::now()),
        syncBypassUntil(createdAt)
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
        (void)videoDev;
        if (syncEnabled) {
            videoFd = ::open(E2_OBH_LIVE_CLOCK_SYSFS, O_RDONLY | O_CLOEXEC);
            if (videoFd < 0)
                audioLog("playback clock unavailable (%s): %s; PCM continues",
                         E2_OBH_LIVE_CLOCK_SYSFS, strerror(errno));
        }
        // DreamTwo/OpenATV-style direct ALSA PCM device (TDM-B, card 0/device 0).
        // Override with OPENBH_AMLOGIC_ALSA_DEVICE for diagnosis if needed.
        const char *selected = getenv("OPENBH_AMLOGIC_ALSA_DEVICE");
        if (!selected || !*selected) selected = "hw:0,0";
        fprintf(stderr, "[OpenBH-AMLAUDIO] opening ALSA device %s (S16_LE, 2 ch, %u Hz)\n", selected, outRate);
        int rc = snd_pcm_open(&pcm, selected, SND_PCM_STREAM_PLAYBACK, 0);
        if (rc < 0) {
            audioLog("ALSA open %s failed: %s", selected, snd_strerror(rc));
            return;
        }
        // Keep the live PCM queue short: V4's 350ms queue plus up to 900ms
        // per-frame waits made audio lag behind video on some DreamTwo units.
        // Lower only if the device remains xrun-free.
        // OPENBH_AMLOGIC_ALSA_LATENCY_US overrides this setting.
        unsigned int latency = 230000;
        const char *latencyEnv = getenv("OPENBH_AMLOGIC_ALSA_LATENCY_US");
        if (latencyEnv && *latencyEnv) {
            char *tail = nullptr;
            long proposed = strtol(latencyEnv, &tail, 10);
            if (tail && !*tail && proposed >= 100000 && proposed <= 1000000)
                latency = (unsigned int)proposed;
        }
        rc = snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE,
                                SND_PCM_ACCESS_RW_INTERLEAVED, 2,
                                outRate, 1, latency);
        if (rc < 0 && latency != 160000) {
            audioLog("ALSA latency %u us rejected (%s); retry 160000 us", latency, snd_strerror(rc));
            rc = snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE,
                                     SND_PCM_ACCESS_RW_INTERLEAVED, 2,
                                     outRate, 1, 160000);
        }
        if (rc < 0) {
            audioLog("ALSA parameters failed: %s", snd_strerror(rc));
            snd_pcm_close(pcm);
            pcm = nullptr;
            return;
        }
        snd_pcm_uframes_t bufferFrames = 0, periodFrames = 0;
        if (snd_pcm_get_params(pcm, &bufferFrames, &periodFrames) == 0) {
            // Prebuffer approximately 80ms to avoid cold-start xruns without
            // introducing V4's large fixed startup delay.
            snd_pcm_sw_params_t *params;
            snd_pcm_sw_params_alloca(&params);
            if (snd_pcm_sw_params_current(pcm, params) == 0) {
                const snd_pcm_uframes_t requested = (snd_pcm_uframes_t)(outRate * 80 / 1000);
                const snd_pcm_uframes_t threshold =
                    std::max(periodFrames, std::min(requested,
                             bufferFrames > periodFrames ? bufferFrames - periodFrames : bufferFrames));
                if (snd_pcm_sw_params_set_start_threshold(pcm, params, threshold) == 0 &&
                    snd_pcm_sw_params(pcm, params) == 0)
                    audioLog("ALSA startup threshold=%lu frames", (unsigned long)threshold);
            }
            audioLog("opened %s, rate=%u, buffer=%lu, period=%lu frames, avsync=%d, softvol=%d",
                     selected, outRate, (unsigned long)bufferFrames,
                     (unsigned long)periodFrames, (int)syncEnabled, (int)softwareVolume);
        } else {
            audioLog("opened %s, rate=%u, avsync=%d", selected, outRate, (int)syncEnabled);
        }
        // A nonblocking PCM write lets channel-change stop() flush instantly
        // instead of joining a thread stuck waiting for a full ALSA buffer.
        if (snd_pcm_nonblock(pcm, 1) < 0)
            audioLog("ALSA nonblocking mode unavailable; zap flush may be delayed");
    }
    // Pausing the TS read loop alone is not an A/V pause: ALSA can still
    // present PCM frames that were queued before VIDEO_FREEZE. Keep those
    // frames in the hardware PCM ring when ALSA supports native pause.
    // In particular, do not drop/prepare/flush or re-anchor audio on resume.
    void setPaused(bool paused)
    {
        if (!pcm)
            return;
        if (paused) {
            snd_pcm_state_t state = snd_pcm_state(pcm);
            if (state == SND_PCM_STATE_PAUSED) {
                pcmPauseActive = true;
                return;
            }
            if (state != SND_PCM_STATE_RUNNING) {
                pcmPauseActive = false;
                audioLog("PAUSE: ALSA state=%s; no running PCM to hold",
                         snd_pcm_state_name(state));
                return;
            }
            int rc = snd_pcm_pause(pcm, 1);
            pcmPauseActive = (rc == 0);
            if (rc < 0)
                audioLog("PAUSE: native ALSA freeze unavailable (%s); "
                         "exact A/V continuity NOT guaranteed", snd_strerror(rc));
            else
                audioLog("PAUSE: PCM queue frozen without clearing samples");
        } else if (pcmPauseActive) {
            int rc = snd_pcm_pause(pcm, 0);
            if (rc < 0) {
                audioLog("RESUME: native ALSA unpause failed (%s); "
                         "PCM recovery required", snd_strerror(rc));
                return;
            }
            pcmPauseActive = false;
            audioLog("RESUME: preserved PCM queue released without PTS reset");
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
    // The kernel ioctl may report a stale timestamp for a stopped decoder.
    // Accept clock data only while its timestamp is actually advancing.
    bool playbackVideoPts(uint64_t &value)
    {
        value = 0;
        if (videoFd < 0 || clockUnsupported) return false;
        ++clockReads;
        char buf[48];
        if (::lseek(videoFd, 0, SEEK_SET) < 0) return false;
        const ssize_t count = ::read(videoFd, buf, sizeof(buf) - 1);
        if (count <= 0) return false;
        buf[count] = 0;
        char *end = nullptr;
        const unsigned long long ticks = strtoull(buf, &end, 10);
        if (end == buf || !ticks || ticks == UINT64_MAX) return false;
        value = (uint64_t)ticks;
        const auto now = std::chrono::steady_clock::now();
        if (value != vptsLast) {
            vptsLast = value;
            vptsProgressAt = now;
        }
        if (vptsProgressAt == std::chrono::steady_clock::time_point() ||
            now - vptsProgressAt > std::chrono::milliseconds(450))
            return false;
        ++clockReady;
        return true;
    }
    void logStats(unsigned long long tsPackets, unsigned long long payloadPackets,
                  unsigned long long ccGaps, unsigned long long duplicatedPackets,
                  unsigned long long pesStarts, unsigned long long reads,
                  unsigned long long pollTimeouts)
    {
        snd_pcm_sframes_t available = pcm ? snd_pcm_avail_update(pcm) : -1;
        audioLog("stats ts=%llu payload=%llu reads=%llu pes=%llu cc_gaps=%llu dup=%llu "
                 "poll_empty=%llu decoded=%llu pcm_frames=%llu xruns=%llu "
                 "write_errors=%llu pts_drops=%llu sync_waits=%llu sync_wait_ms=%llu "
                 "sync_timeouts=%llu "
                 "volume=%d mute=%d auto_error_ms=%ld clock=%llu/%llu "
                 "auto_updates=%llu adjust_samples=%d startup_discards=%llu "
                 "alsa_state=%s avail=%ld",
                 tsPackets, payloadPackets, reads, pesStarts, ccGaps, duplicatedPackets,
                 pollTimeouts, decodedFrames, pcmFramesWritten, xruns, writeErrors,
                 ptsDrops, syncWaits, syncWaitMs, syncTimeouts,
                 lastAppliedVolume, (int)lastAppliedMute, lastAvDeltaMs,
                 clockReady, clockReads, autoUpdates, lastCompensation, startupDiscarded,
                 pcm ? snd_pcm_state_name(snd_pcm_state(pcm)) : "NONE",
                 (long)available);
    }
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
        createdAt = std::chrono::steady_clock::now();
        syncBypassUntil = createdAt;
        initialSyncDone = false;
        startupSilenceMs = 0;
        startupSkipMs = 0;
        lastSeenParserPts = AV_NOPTS_VALUE;
        vptsLast = 0;
        vptsProgressAt = std::chrono::steady_clock::time_point();
        lastServoAt = std::chrono::steady_clock::time_point();
        filteredError = 0.0;
        autoUpdates = 0;
        lastCompensation = 0;
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
        // Only re-anchor on a NEW PES PTS, not on every parsed frame.
        // V4 anchored once at channel start and then drifted on discontinuity.
        if (parser && parser->pts != AV_NOPTS_VALUE &&
            parser->pts != lastSeenParserPts) {
            const int64_t newPts = parser->pts & ((1LL << 33) - 1);
            lastSeenParserPts = parser->pts;
            if (lastPts < 0 || std::llabs(ptsDelta(newPts, lastPts)) > 9000) {
                if (lastPts >= 0)
                    audioLog("PES timestamp re-anchor old=%lld new=%lld delta=%lldms",
                         (long long)lastPts, (long long)newPts,
                         (long long)(ptsDelta(newPts, lastPts) / 90));
                lastPts = newPts;
            }
        }
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
        // One clock sample per decoded block; never change vendor PCR, tsync,
        // VIDEO_GET_PTS, demux ownership, or video decoder controls.
        uint64_t videoPts = 0;
        const bool videoClockReady = syncEnabled && playbackVideoPts(videoPts);
        // Compare the head of the PCM that is ACTUALLY audible (minus ALSA
        // queue), not the timestamp of the latest submitted TS packet.
        if (videoClockReady && initialSyncDone && lastPts >= 0 && pcm &&
            snd_pcm_state(pcm) == SND_PCM_STATE_RUNNING) {
            snd_pcm_sframes_t queue = 0;
            if (snd_pcm_delay(pcm, &queue) == 0 && queue >= 0 &&
                queue <= (snd_pcm_sframes_t)(outRate * 2)) {
                const int64_t playPts = lastPts - (int64_t)queue * 90000 / outRate;
                const int64_t errorPts = ptsDelta(playPts, (int64_t)videoPts);
                // Reject stale epochs or PCR discontinuities during zapping.
                if (errorPts > -45000 && errorPts < 45000) {
                    const double e = (double)errorPts / 90.0;
                    filteredError = autoUpdates ? (filteredError * 0.85 + e * 0.15) : e;
                    lastAvDeltaMs = (long)filteredError;
                    const auto now = std::chrono::steady_clock::now();
                    if (lastServoAt == std::chrono::steady_clock::time_point() ||
                        now - lastServoAt >= std::chrono::milliseconds(500)) {
                        // Correct steadily through FFmpeg's resampler; audio
                        // ahead => add output samples (slower), behind =>
                        // remove samples (faster). Keep variation below 2%.
                        int samples = 0;
                        if (filteredError > 20.0 || filteredError < -20.0) {
                            samples = (int)(filteredError * (double)outRate / 5000.0);
                            const int bound = (int)outRate / 50;
                            samples = std::max(-bound, std::min(bound, samples));
                        }
                        if (swr_set_compensation(swr, samples, (int)outRate) >= 0) {
                            lastCompensation = samples;
                            ++autoUpdates;
                        }
                        lastServoAt = now;
                    }
                }
            }
        }
        // Account for swr's dynamic compensation: output may exceed a plain
        // sample-rate conversion estimate when the controller slows audio.
        int capacity = swr_get_out_samples(swr, frame->nb_samples);
        if (capacity <= 0 || capacity > 32768) return;
        std::vector<int16_t> pcmData((size_t)capacity * 2);
        uint8_t *dst = reinterpret_cast<uint8_t *>(pcmData.data());
        const uint8_t **src = (const uint8_t **)frame->extended_data;
        int frames = swr_convert(swr, &dst, capacity, src, frame->nb_samples);
        if (frames <= 0) return;

        // PCM volume/mute: the GUI changes eDVBVolumecontrol, while the new
        // hw:0,0 output bypasses the old /dev/dvb/adapter*/audio* volume ioctl.
        // Apply the active Enigma2 level directly to the decoded PCM instead.
        // This is intentionally limited to the DREAMNEXTGEN DVB audio worker.
        if (softwareVolume) {
            eDVBVolumecontrol *volume = eDVBVolumecontrol::getInstance();
            if (volume) {
                const int level = std::max(0, std::min(100, volume->getVolume()));
                const bool muted = volume->isMuted();
                if (level != lastAppliedVolume || muted != lastAppliedMute) {
                    audioLog("software volume=%d mute=%d", level, (int)muted);
                    lastAppliedVolume = level;
                    lastAppliedMute = muted;
                }
                const int gain = muted ? 0 : level;
                for (int i = 0; i < frames * 2; ++i) {
                    const int32_t v = (int32_t)pcmData[(size_t)i] * gain / 100;
                    pcmData[(size_t)i] = (int16_t)v;
                }
            }
        }

        // Start on the current picture's PTS, not an incoming video packet.
        // The worker never repeatedly sleeps on video PTS. Until the first
        // picture advances, discard startup PCM instead of queuing seconds
        // of sound against a black screen. Fall back after a short timeout
        // or immediately if the optional driver interface is unavailable.
        if (!initialSyncDone) {
            const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - createdAt).count();
            if (syncEnabled && !clockUnsupported && videoFd >= 0 &&
                !videoClockReady && elapsed < 2000) {
                ++startupDiscarded;
                ++decodedFrames;
                if (lastPts >= 0) {
                    lastPts += (int64_t)frame->nb_samples * 90000 / inputRate;
                    ptsRef.store(lastPts);
                }
                return;
            }
            initialSyncDone = true;
            if (videoClockReady && lastPts >= 0) {
                const int64_t delta90k = ptsDelta(lastPts, (int64_t)videoPts);
                if (delta90k > -45000 && delta90k < 45000) {
                    // One-time correction bounded to avoid stutter; feedback
                    // then follows playback continuously with no fixed offset.
                    const long differenceMs = (long)(delta90k / 90);
                    startupSilenceMs = std::max(0L, std::min(100L, differenceMs));
                    startupSkipMs = std::max(0L, std::min(250L, -differenceMs));
                }
            }
            audioLog("automatic startup vpts=%llu audio_pts=%lld silence=%ld skip=%ld clock=%d",
                (unsigned long long)videoPts, (long long)lastPts,
                startupSilenceMs, startupSkipMs, (int)videoClockReady);
        }
        // If audio is late relative to video, advance it by skipping only
        // startup PCM; do not repeatedly chop decoded audio mid-program.
        if (startupSkipMs > 0 && frames > 0) {
            const int skip = (int)std::min((long)frames,
                             (startupSkipMs * (long)outRate + 999) / 1000);
            if (skip > 0) {
                pcmData.erase(pcmData.begin(), pcmData.begin() + skip * 2);
                frames -= skip;
                startupSkipMs -= (long)skip * 1000 / outRate;
                if (startupSkipMs <= 1) startupSkipMs = 0;
            }
            // The frame's timeline advances below whether we output or skip.
            if (frames == 0) {
                ++decodedFrames;
                if (lastPts >= 0) {
                    lastPts += (int64_t)frame->nb_samples * 90000 / inputRate;
                    ptsRef.store(lastPts);
                }
                return;
            }
        }
        if (startupSilenceMs > 0) {
            const size_t silence = (size_t)startupSilenceMs * outRate / 1000;
            pcmData.insert(pcmData.begin(), silence * 2, 0);
            frames += (int)silence;
            startupSilenceMs = 0;
        }
        int offset = 0;
        int recoveryTries = 0;
        auto lastProgress = std::chrono::steady_clock::now();
        while (offset < frames && !stopFlag.load()) {
            snd_pcm_sframes_t n = snd_pcm_writei(pcm, pcmData.data() + offset * 2,
                                                 (snd_pcm_uframes_t)(frames - offset));
            if (n == -EINTR) continue;
            if (n == -EAGAIN) {
                if (std::chrono::steady_clock::now() - lastProgress >
                    std::chrono::milliseconds(700)) {
                    ++writeErrors;
                    audioLog("ALSA nonblocking write timed out (700 ms)");
                    break;
                }
                usleep(4000);
                continue;
            }
            if (n < 0) {
                if (n == -EPIPE || n == -ESTRPIPE) ++xruns;
                int recovered = snd_pcm_recover(pcm, (int)n, 1);
                if (recovered >= 0 && ++recoveryTries <= 3)
                    continue;
                ++writeErrors;
                audioLog("ALSA write error %ld (%s), recover=%d",
                         (long)n, snd_strerror((int)n), recovered);
                break;
            }
            if (n == 0) break;
            recoveryTries = 0;
            offset += (int)n;
            lastProgress = std::chrono::steady_clock::now();
            pcmFramesWritten += (unsigned long long)n;
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
    std::atomic<bool> &stopFlag;
    unsigned long long decodedFrames;
    unsigned long long pcmFramesWritten;
    unsigned long long xruns;
    unsigned long long writeErrors;
    unsigned long long ptsDrops;
    unsigned long long syncWaits;
    unsigned long long syncWaitMs;
    unsigned long long syncTimeouts = 0;
    bool initialSyncDone;
    long startupSilenceMs;
    long startupSkipMs;
    long lastAvDeltaMs;
    int64_t lastSeenParserPts;
    bool syncEnabled;
    bool softwareVolume;
    int lastAppliedVolume;
    bool lastAppliedMute;
    std::chrono::steady_clock::time_point createdAt;
    std::chrono::steady_clock::time_point syncBypassUntil;
    bool clockUnsupported;
    uint64_t vptsLast;
    unsigned long long clockReads, clockReady, autoUpdates;
    int lastCompensation;
    unsigned long long startupDiscarded;
    bool pcmPauseActive = false;
    double filteredError;
    std::chrono::steady_clock::time_point vptsProgressAt;
    std::chrono::steady_clock::time_point lastServoAt;
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
    // Signal the worker before stopping the PID. Nonblocking ALSA writes and
    // bounded sync waits let the destructor drop queued old-channel PCM.
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
    AudioWorker worker(m_type, m_videoDev, m_pts, m_stop);
    if (!worker.ready()) { notice("ALSA/FFmpeg worker init failed"); return; }
    // TSDEMUX_TAP returns multiplexed TS, not elementary-stream PCM.
    std::vector<uint8_t> input;
    input.reserve(64 * 1024);
    bool havePes = false;
    uint8_t lastCC = 0;
    bool haveCC = false;
    unsigned long long tsPackets = 0, payloadPackets = 0, ccGaps = 0;
    unsigned long long duplicatedPackets = 0, pesStarts = 0, reads = 0, pollTimeouts = 0;
    time_t lastStats = time(nullptr);
    uint8_t buf[188 * 128];
    bool workerPauseState = false;
    while (!m_stop.load()) {
        if (m_flush.exchange(false)) {
            worker.reset();
            input.clear();
            havePes = false;
            haveCC = false;
        }
        /* Only the PCM owner thread touches ALSA pause state; keep the
         * queued PCM samples across timeshift freeze/continue when supported. */
        const bool requestedPause = m_paused.load();
        if (requestedPause != workerPauseState) {
            worker.setPaused(requestedPause);
            workerPauseState = requestedPause;
        }
        if (requestedPause) { usleep(5000); continue; }
        pollfd pfd = {m_demuxFd, POLLIN | POLLERR | POLLHUP, 0};
        int r = ::poll(&pfd, 1, 100);
        if (r <= 0 || !(pfd.revents & POLLIN)) {
            ++pollTimeouts;
        } else {
            ssize_t got = ::read(m_demuxFd, buf, sizeof(buf));
            if (got > 0) {
                ++reads;
                input.insert(input.end(), buf, buf + got);
            }
        }
        size_t pos = 0;
        while (input.size() - pos >= 188) {
            if (input[pos] != 0x47) { ++pos; continue; }
            // Require another sync before consuming a packet when possible.
            if (input.size() - pos >= 376 && input[pos + 188] != 0x47) { ++pos; continue; }
            const uint8_t *ts = input.data() + pos;
            pos += 188;
            ++tsPackets;
            int pid = ((ts[1] & 0x1f) << 8) | ts[2];
            if (pid != m_pid || (ts[1] & 0x80)) continue;
            int afc = (ts[3] >> 4) & 3;
            if (afc != 1 && afc != 3) continue;
            uint8_t cc = ts[3] & 0x0f;
            bool pusi = (ts[1] & 0x40) != 0;
            bool discontinuity = afc == 3 && ts[4] >= 1 &&
                                 (ts[5] & 0x80) != 0;
            if (discontinuity) {
                haveCC = false;
                havePes = false;
            }
            if (haveCC && cc == lastCC) {
                // A duplicate payload TS packet must not reset the decoder.
                ++duplicatedPackets;
                continue;
            }
            if (haveCC && cc != (uint8_t)((lastCC + 1) & 15)) {
                ++ccGaps;
                // Discard only the damaged PES remainder. Do NOT drop/prepare
                // ALSA or flush the whole codec on a transient TS gap.
                havePes = false;
            }
            lastCC = cc; haveCC = true;
            ++payloadPackets;
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
                ++pesStarts;
            }
            if (havePes && offset < 188)
                worker.submit(ts + offset, 188 - offset);
        }
        if (pos) input.erase(input.begin(), input.begin() + (std::ptrdiff_t)pos);
        if (input.size() > 256 * 1024) {
            input.clear(); havePes = false; haveCC = false;
            audioLog("TS buffer overflow: dropped incomplete packet sequence");
        }
        time_t now = time(nullptr);
        if (now - lastStats >= 5) {
            worker.logStats(tsPackets, payloadPackets, ccGaps, duplicatedPackets,
                            pesStarts, reads, pollTimeouts);
            lastStats = now;
        }
    }
}
#endif // DREAMNEXTGEN
