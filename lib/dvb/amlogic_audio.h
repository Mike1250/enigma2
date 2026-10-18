#ifndef OPENBH_AMLOGIC_AUDIO_H
#define OPENBH_AMLOGIC_AUDIO_H

#ifdef DREAMNEXTGEN
#include <atomic>
#include <pthread.h>
#include <stdint.h>

/* A DVB TS -> FFmpeg -> ALSA PCM audio pipeline for DreamOne/Two.
 * This is deliberately independent of the legacy audiodsp char driver. */
class OpenBHAmlogicAudio
{
public:
    OpenBHAmlogicAudio(int demuxFd, int videoDev);
    ~OpenBHAmlogicAudio();
    int start(int audioPid, int enigmaAudioType);
    void stop();
    void pause(bool paused);
    void flush();
    int64_t pts() const { return m_pts.load(); }
private:
    OpenBHAmlogicAudio(const OpenBHAmlogicAudio &) = delete;
    OpenBHAmlogicAudio &operator=(const OpenBHAmlogicAudio &) = delete;
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
