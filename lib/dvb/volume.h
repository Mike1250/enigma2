#ifndef __volume_h
#define __volume_h

#ifdef HAVE_ALSA
#include <alsa/asoundlib.h>
#endif

#include <lib/base/ebase.h>
#ifndef SWIG
#include <sigc++/sigc++.h>
#endif

class eDVBVolumecontrol
{
private:
#ifdef HAVE_ALSA
	snd_mixer_elem_t *mainVolume;
	snd_mixer_t *alsaMixerHandle;
#endif
	static eDVBVolumecontrol *instance;
	eDVBVolumecontrol();
#ifdef SWIG
	~eDVBVolumecontrol();
#endif
	int openMixer();
	void closeMixer(int fd);

	bool muted;
	int leftVol, rightVol;
	int m_volsteps;

#ifndef SWIG
#ifndef HAVE_ALSA
	/* Native, in-process IPAudioPlus GStreamer session watcher. */
	ePtr<eTimer> m_ipaNativeTimer;
	sigc::connection m_ipaNativeConnection;
	void ipaNativeTick();
#endif
#endif

	int checkVolume(int vol);

public:
#ifndef SWIG
	/* Notification only, never changes the DVB hardware mixer path. */
	sigc::signal<void()> volumeChanged;
#endif
	static eDVBVolumecontrol* getInstance();

	void setVolumeSteps(int steps);
	void volumeUp(int left = 0, int right = 0);
	void volumeDown(int left = 0, int right = 0);

	void setVolume(int left, int right);

	void volumeMute();
	void volumeUnMute();
	void volumeToggleMute();

	int getVolume();
	bool isMuted();
};

#endif //__volume_h
