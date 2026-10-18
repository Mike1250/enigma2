#ifndef DREAMNEXTGEN_AUDIO_H
#define DREAMNEXTGEN_AUDIO_H

#ifdef DREAMNEXTGEN
#include <atomic>
#include <pthread.h>
#include <stdint.h>

/* A DVB TS -> FFmpeg -> ALSA PCM audio pipeline for DreamOne/Two.
 * This is deliberately independent of the legacy audiodsp char driver. */
class DreamNextGenAudio
{
public:
    DreamNextGenAudio(int demuxFd, int videoDev);
    ~DreamNextGenAudio();
    int start(int audioPid, int enigmaAudioType);
    void stop();
    void pause(bool paused);
    void flush();
    int64_t pts() const { return m_pts.load(); }
private:
    DreamNextGenAudio(const DreamNextGenAudio &) = delete;
    DreamNextGenAudio &operator=(const DreamNextGenAudio &) = delete;
    static void *runThread(void *arg);
    void run();
    int m_demuxFd;
    int m_videoDev;
    int m_pid;
    int m_type;
    pthread_t m_thread;
    bool m_started;
    std::atomic<bool> m_stop;
    std::atomic<bool> m_paused;
    std::atomic<bool> m_flush;
    std::atomic<int64_t> m_pts;
};
#endif
#endif
