#include <lib/base/cfile.h>
#include <lib/base/eerror.h>
#include <lib/dvb/volume.h>
#include <stdio.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>
#if defined(DREAMNEXTGEN) && !defined(HAVE_ALSA)
#include <alsa/asoundlib.h>
#include <dirent.h>
#include <ctype.h>
#include <string.h>
#endif

#include <linux/dvb/version.h>
#if DVB_API_VERSION < 3
#define VIDEO_DEV "/dev/dvb/card0/video0"
#define AUDIO_DEV "/dev/dvb/card0/audio0"
#include <ost/audio.h>
#include <ost/video.h>
#else
#define VIDEO_DEV "/dev/dvb/adapter0/video0"
#define AUDIO_DEV "/dev/dvb/adapter0/audio0"
#include <linux/dvb/audio.h>
#include <linux/dvb/video.h>
#endif

#ifdef HAVE_ALSA
# ifndef ALSA_VOLUME_MIXER
#  define ALSA_VOLUME_MIXER "Master"
# endif
# ifndef ALSA_CARD
#  define ALSA_CARD "default"
# endif
#endif

// Optional OpenATV-style ALSA mixer mirror for IPAudioPlus on DreamNextGen.
// Unlike enabling HAVE_ALSA globally, this does not replace OpenBH DVB volume.
// Requires both an explicit marker file and an active ipa_core process.
#if defined(DREAMNEXTGEN) && !defined(HAVE_ALSA)
static bool s_ipa_alsa_bridge_ready = false;

static bool e2IPACoreIsRunning()
{
    DIR *dir = opendir("/proc");
    if (!dir)
        return false;
    bool found = false;
    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL)
    {
        if (!isdigit((unsigned char)ent->d_name[0]))
            continue;
        char path[128];
        snprintf(path, sizeof(path), "/proc/%s/comm", ent->d_name);
        FILE *fd = fopen(path, "r");
        if (!fd)
            continue;
        char name[64] = {0};
        const bool read_ok = fgets(name, sizeof(name), fd) != NULL;
        fclose(fd);
        if (read_ok && (!strcmp(name, "ipa_core\n") || !strcmp(name, "ipa_core")))
        {
            found = true;
            break;
        }
    }
    closedir(dir);
    return found;
}

static void e2IPAMirrorAlsaVolume(int percent)
{
    // Disabled by default and only applies while the IPA standalone player runs.
    if (access("/etc/enigma2/ipa_alsa_mirror.enabled", F_OK) != 0 || !e2IPACoreIsRunning())
        return;
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    snd_mixer_t *handle = NULL;
    if (snd_mixer_open(&handle, 0) < 0)
        return;
    const char *card = "hw:0";
    if (snd_mixer_attach(handle, card) < 0 ||
        snd_mixer_selem_register(handle, NULL, NULL) < 0 ||
        snd_mixer_load(handle) < 0)
    {
        snd_mixer_close(handle);
        return;
    }
    snd_mixer_selem_id_t *sid;
    snd_mixer_selem_id_alloca(&sid);
    snd_mixer_selem_id_set_name(sid, "Master");
    snd_mixer_selem_id_set_index(sid, 0);
    snd_mixer_elem_t *master = snd_mixer_find_selem(handle, sid);
    if (master && snd_mixer_selem_has_playback_volume(master))
    {
        long min_raw = 0, max_raw = 0;
        if (snd_mixer_selem_get_playback_volume_range(master, &min_raw, &max_raw) == 0 && max_raw > min_raw)
        {
            long raw = min_raw + (max_raw - min_raw) * percent / 100;
            snd_mixer_selem_set_playback_volume_all(master, raw);
        }
    }
    snd_mixer_close(handle);
}
#endif // DREAMNEXTGEN && !HAVE_ALSA

eDVBVolumecontrol* eDVBVolumecontrol::instance = NULL;

eDVBVolumecontrol* eDVBVolumecontrol::getInstance()
{
	if (instance == NULL)
		instance = new eDVBVolumecontrol;
	return instance;
}

eDVBVolumecontrol::eDVBVolumecontrol()
:m_volsteps(5)
{
#ifdef HAVE_ALSA
	mainVolume = NULL;
	openMixer();
#endif
	volumeUnMute();
	setVolume(100, 100);
}

int eDVBVolumecontrol::openMixer()
{
#ifdef HAVE_ALSA
	if (!mainVolume)
	{
		int err;
		char *card = ALSA_CARD;

		eDebug("[eDVBVolumecontrol] Setup ALSA Mixer %s - %s", ALSA_CARD, ALSA_VOLUME_MIXER);
		/* Perform the necessary pre-amble to start up ALSA Mixer */
		err = snd_mixer_open(&alsaMixerHandle, 0);
		if (err < 0)
		{
			eDebug("[eDVBVolumecontrol] Mixer %s open error: %s", card, snd_strerror(err));
			return err;
		}
		err = snd_mixer_attach(alsaMixerHandle, card);
		if (err < 0)
		{
			eDebug("[eDVBVolumecontrol] Mixer attach %s error: %s", card, snd_strerror(err));
			snd_mixer_close(alsaMixerHandle);
			alsaMixerHandle = NULL;
			return err;
		}
		err = snd_mixer_selem_register(alsaMixerHandle, NULL, NULL);
		if (err < 0)
		{
			eDebug("[eDVBVolumecontrol] Mixer register error: %s", snd_strerror(err));
			snd_mixer_close(alsaMixerHandle);
			alsaMixerHandle = NULL;
			return err;
		}
		err = snd_mixer_load(alsaMixerHandle);
		if (err < 0)
		{
			eDebug("[eDVBVolumecontrol] Mixer %s load error: %s", card, snd_strerror(err));
			snd_mixer_close(alsaMixerHandle);
			alsaMixerHandle = NULL;
			return err;
		}

		/* Set up Decoder 0 as the main volume control. */
		snd_mixer_selem_id_t *sid;
		snd_mixer_selem_id_alloca(&sid);
		snd_mixer_selem_id_set_name(sid, ALSA_VOLUME_MIXER);
		snd_mixer_selem_id_set_index(sid, 0);
		mainVolume = snd_mixer_find_selem(alsaMixerHandle, sid);
	}
	return mainVolume ? 0 : -1;
#else
	return open( AUDIO_DEV, O_RDWR );
#endif
}

void eDVBVolumecontrol::closeMixer(int fd)
{
#ifdef HAVE_ALSA
	/* we want to keep the alsa mixer */
#else
	if (fd >= 0) close(fd);
#endif
}

void eDVBVolumecontrol::setVolumeSteps(int steps)
{
	m_volsteps = steps;
}

void eDVBVolumecontrol::volumeUp(int left, int right)
{
	setVolume(leftVol + (left ? left : m_volsteps), rightVol + (right ? right : m_volsteps));
}

void eDVBVolumecontrol::volumeDown(int left, int right)
{
	setVolume(leftVol - (left ? left : m_volsteps), rightVol - (right ? right : m_volsteps));
}

int eDVBVolumecontrol::checkVolume(int vol)
{
	if (vol < 0)
		vol = 0;
	else if (vol > 100)
		vol = 100;
	return vol;
}

void eDVBVolumecontrol::setVolume(int left, int right)
{
		/* left, right is 0..100 */
	leftVol = checkVolume(left);
	rightVol = checkVolume(right);
	volumeChanged.emit();
#if defined(DREAMNEXTGEN) && !defined(HAVE_ALSA)
	s_ipa_alsa_bridge_ready = true;
	e2IPAMirrorAlsaVolume(muted ? 0 : leftVol);
#endif

#ifdef HAVE_ALSA
	eDebug("[eDVBVolumecontrol] Setvolume: ALSA leftVol=%d", leftVol);
	if (mainVolume)
		snd_mixer_selem_set_playback_volume_all(mainVolume, muted ? 0 : leftVol);
#else
		/* convert to -1dB steps */
	left = 63 - leftVol * 63 / 100;
	right = 63 - rightVol * 63 / 100;
		/* now range is 63..0, where 0 is loudest */

	audio_mixer_t mixer;

	mixer.volume_left = left;
	mixer.volume_right = right;

	eDebug("[eDVBVolumecontrol] Setvolume: raw: %d %d, -1db: %d %d", leftVol, rightVol, left, right);

	int fd = openMixer();
	if (fd >= 0)
	{
#ifdef DVB_API_VERSION
		if (ioctl(fd, AUDIO_SET_MIXER, &mixer) < 0) {
			eDebug("[eDVBVolumecontrol] Setvolume failed: %m");
		}
#endif
		closeMixer(fd);
		return;
	}
	else {
		eDebug("[eDVBVolumecontrol] SetVolume failed to open mixer: %m");
	}

	//HACK?
	CFile::writeInt("/proc/stb/avs/0/volume", left); /* in -1dB */
#endif
}

int eDVBVolumecontrol::getVolume()
{
	return leftVol;
}

bool eDVBVolumecontrol::isMuted()
{
	return muted;
}


void eDVBVolumecontrol::volumeMute()
{
#ifdef HAVE_ALSA
	eDebug("[eDVBVolumecontrol] Setvolume: ALSA Mute");
	if (mainVolume)
		snd_mixer_selem_set_playback_volume_all(mainVolume, 0);
	muted = true;
#else
	int fd = openMixer();
	if (fd >= 0)
	{
#ifdef DVB_API_VERSION
		ioctl(fd, AUDIO_SET_MUTE, true);
#endif
		closeMixer(fd);
	}
	muted = true;

	//HACK?
	CFile::writeInt("/proc/stb/audio/j1_mute", 1);
#endif
	volumeChanged.emit();
#if defined(DREAMNEXTGEN) && !defined(HAVE_ALSA)
	if (s_ipa_alsa_bridge_ready)
		e2IPAMirrorAlsaVolume(0);
#endif
}

void eDVBVolumecontrol::volumeUnMute()
{
#ifdef HAVE_ALSA
	eDebug("[eDVBVolumecontrol] Setvolume: ALSA unMute to %d", leftVol);
	if (mainVolume)
		snd_mixer_selem_set_playback_volume_all(mainVolume, leftVol);
	muted = false;
#else
	int fd = openMixer();
	if (fd >= 0)
	{
#ifdef DVB_API_VERSION
		ioctl(fd, AUDIO_SET_MUTE, false);
#endif
		closeMixer(fd);
	}
	muted = false;

	//HACK?
	CFile::writeInt("/proc/stb/audio/j1_mute", 0);
#endif
	volumeChanged.emit();
#if defined(DREAMNEXTGEN) && !defined(HAVE_ALSA)
	if (s_ipa_alsa_bridge_ready)
		e2IPAMirrorAlsaVolume(leftVol);
#endif
}

void eDVBVolumecontrol::volumeToggleMute()
{
	if (isMuted())
		volumeUnMute();
	else
		volumeMute();
}
