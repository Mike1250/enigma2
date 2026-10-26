#include <lib/base/cfile.h>
#include <lib/base/eerror.h>
#include <lib/dvb/volume.h>
#include <stdio.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>
/* OpenBH IPAudioPlus V2: native, session-scoped OpenATV-style Master volume.
 * Active only for the GStreamer audio-only playbin used by IPAudioPlus.
 * The dlopen path avoids changing existing Enigma2 ALSA link flags.
 * This section is intentionally built only for the non-HAVE_ALSA OpenBH
 * volume backend; that backend retains its DVB volume behavior unchanged.
 */
#ifndef HAVE_ALSA
#include <dirent.h>
#include <dlfcn.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

namespace {
static const char * const ipaNativeFlag = "/etc/enigma2/ipa_native_volume.enabled";

static bool ipaNativeEnabled()
{
    return access(ipaNativeFlag, F_OK) == 0;
}

/* Match exact IPAudioPlus audio-only gst-launch signature, not generic video.
 * The private URL remains in /proc only and is never logged or copied out.
 */
static bool ipaNativePlayerActive()
{
    DIR *dir = opendir("/proc");
    if (!dir) return false;
    bool active = false;
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL && !active)
    {
        if (entry->d_name[0] < '0' || entry->d_name[0] > '9') continue;
        char *end = NULL;
        const unsigned long pid = strtoul(entry->d_name, &end, 10);
        if (!end || *end != '\0' || pid == 0) continue;
        char file[96];
        snprintf(file, sizeof(file), "/proc/%lu/cmdline", pid);
        int fd = open(file, O_RDONLY | O_CLOEXEC);
        if (fd < 0) continue;
        char cmd[16384];
        const ssize_t n = read(fd, cmd, sizeof(cmd));
        close(fd);
        if (n <= 0) continue;
        /* GStreamer IPAudioPlus may format playbin flags differently on OpenBH;
         * identify the audio-only ALSA playbin by its defining arguments. */
        bool gst = false, playbin = false, video = false, audio = false;
        size_t offset = 0;
        bool first = true;
        while (offset < static_cast<size_t>(n))
        {
            const char *arg = cmd + offset;
            const size_t avail = static_cast<size_t>(n) - offset;
            const size_t len = strnlen(arg, avail);
            if (len == avail) break;
            if (first)
            {
                const char *base = strrchr(arg, '/');
                base = base ? base + 1 : arg;
                gst = strcmp(base, "gst-launch-1.0") == 0;
                if (!gst) break;
                first = false;
            }
            else if (strcmp(arg, "playbin") == 0) playbin = true;
            else if (strcmp(arg, "video-sink=fakesink") == 0) video = true;
            else if (strncmp(arg, "audio-sink=alsasink", 19) == 0) audio = true;
            offset += len + 1;
        }
        active = gst && playbin && video && audio;
    }
    closedir(dir);
    return active;
}

/* No build-system or header dependency on ALSA: resolve public mixer ABI. */
struct IpaNativeAlsa
{
    void *lib;
    void *handle;
    void *master;
    int (*mixer_open)(void **, int);
    int (*mixer_attach)(void *, const char *);
    int (*mixer_register)(void *, void *, void *);
    int (*mixer_load)(void *);
    int (*mixer_close)(void *);
    void *(*first_elem)(void *);
    void *(*next_elem)(void *);
    const char *(*elem_name)(void *);
    unsigned int (*elem_index)(void *);
    int (*has_volume)(void *);
    int (*get_volume)(void *, int, long *);
    int (*set_volume)(void *, int, long);
    int (*set_all)(void *, long);
    int (*get_range)(void *, long *, long *);
    int (*get_db_range)(void *, long *, long *);
    int (*set_db_all)(void *, long, int);
    long min_raw, max_raw, min_db, max_db;
    bool has_db;

    IpaNativeAlsa() : lib(NULL), handle(NULL), master(NULL),
        mixer_open(NULL), mixer_attach(NULL), mixer_register(NULL), mixer_load(NULL),
        mixer_close(NULL), first_elem(NULL), next_elem(NULL), elem_name(NULL),
        elem_index(NULL), has_volume(NULL), get_volume(NULL), set_volume(NULL),
        set_all(NULL), get_range(NULL), get_db_range(NULL), set_db_all(NULL),
        min_raw(0), max_raw(0), min_db(0), max_db(0), has_db(false) {}
    ~IpaNativeAlsa() { close(); }
    void close()
    {
        master = NULL;
        if (handle && mixer_close) mixer_close(handle);
        handle = NULL;
        if (lib) dlclose(lib);
        lib = NULL;
    }
    bool open()
    {
        if (master) return true;
        if (!lib)
        {
            lib = dlopen("libasound.so.2", RTLD_NOW | RTLD_LOCAL);
            if (!lib) return false;
#define IPA_DLSYM(field, symbol) \
            field = reinterpret_cast<decltype(field)>(dlsym(lib, symbol)); \
            if (!field) { close(); return false; }
            IPA_DLSYM(mixer_open, "snd_mixer_open")
            IPA_DLSYM(mixer_attach, "snd_mixer_attach")
            IPA_DLSYM(mixer_register, "snd_mixer_selem_register")
            IPA_DLSYM(mixer_load, "snd_mixer_load")
            IPA_DLSYM(mixer_close, "snd_mixer_close")
            IPA_DLSYM(first_elem, "snd_mixer_first_elem")
            IPA_DLSYM(next_elem, "snd_mixer_elem_next")
            IPA_DLSYM(elem_name, "snd_mixer_selem_get_name")
            IPA_DLSYM(elem_index, "snd_mixer_selem_get_index")
            IPA_DLSYM(has_volume, "snd_mixer_selem_has_playback_volume")
            IPA_DLSYM(get_volume, "snd_mixer_selem_get_playback_volume")
            IPA_DLSYM(set_volume, "snd_mixer_selem_set_playback_volume")
            IPA_DLSYM(set_all, "snd_mixer_selem_set_playback_volume_all")
            IPA_DLSYM(get_range, "snd_mixer_selem_get_playback_volume_range")
            IPA_DLSYM(get_db_range, "snd_mixer_selem_get_playback_dB_range")
            IPA_DLSYM(set_db_all, "snd_mixer_selem_set_playback_dB_all")
#undef IPA_DLSYM
        }
        if (mixer_open(&handle, 0) < 0 ||
            mixer_attach(handle, "hw:0") < 0 ||
            mixer_register(handle, NULL, NULL) < 0 ||
            mixer_load(handle) < 0)
        {
            close();
            return false;
        }
        for (void *elem = first_elem(handle); elem; elem = next_elem(elem))
        {
            const char *name = elem_name(elem);
            if (name && strcmp(name, "Master") == 0 &&
                elem_index(elem) == 0 && has_volume(elem))
            {
                master = elem;
                break;
            }
        }
        if (!master || get_range(master, &min_raw, &max_raw) < 0 ||
            max_raw <= min_raw)
        {
            close();
            return false;
        }
        has_db = get_db_range(master, &min_db, &max_db) == 0 && max_db > min_db;
        return true;
    }
    bool read(long &left, long &right)
    {
        return master && get_volume(master, 0, &left) >= 0 &&
            get_volume(master, 1, &right) >= 0;
    }
    bool restore(long left, long right)
    {
        return master && set_volume(master, 0, left) >= 0 &&
            set_volume(master, 1, right) >= 0;
    }
    bool apply(int level)
    {
        if (!master) return false;
        if (level <= 0) return set_all(master, min_raw) >= 0;
        if (level >= 100) return set_all(master, max_raw) >= 0;
        if (has_db)
        {
            long db = static_cast<long>(2000.0 * log10(static_cast<double>(level) / 100.0));
            if (db < min_db) db = min_db;
            if (db > max_db) db = max_db;
            return set_db_all(master, db, 0) >= 0;
        }
        const long value = min_raw + (max_raw - min_raw) * level / 100;
        return set_all(master, value) >= 0;
    }
};

struct IpaNativeSession
{
    IpaNativeAlsa alsa;
    bool active;
    long saved_l, saved_r, written_l, written_r;
    int last_level;
    IpaNativeSession(): active(false), saved_l(0), saved_r(0),
        written_l(0), written_r(0), last_level(-1) {}
    ~IpaNativeSession() { finish(); }
    void finish()
    {
        if (!active) return;
        long current_l, current_r;
        /* Only restore levels if nobody else altered Master meanwhile. */
        if (alsa.read(current_l, current_r) &&
            current_l == written_l && current_r == written_r)
        {
            if (alsa.restore(saved_l, saved_r))
                eDebug("[IPA Native Master] Restored pre-IPAudioPlus ALSA Master");
        }
        active = false;
        last_level = -1;
        alsa.close();
    }
    void sync(int level, bool present)
    {
        if (!ipaNativeEnabled() || !present)
        {
            finish();
            return;
        }
        if (!active)
        {
            if (!alsa.open() || !alsa.read(saved_l, saved_r))
            {
                eDebug("[IPA Native Master] Cannot attach to ALSA Master");
                alsa.close();
                return;
            }
            active = true;
            last_level = -1;
            eDebug("[IPA Native Master] GStreamer IPAudioPlus detected");
        }
        if (level == last_level) return;
        if (alsa.apply(level) && alsa.read(written_l, written_r))
        {
            last_level = level;
            eDebug("[IPA Native Master] Applied %d%%", level);
        }
        else
            eDebug("[IPA Native Master] ALSA Master set failed");
    }
};
static IpaNativeSession ipaNativeSession;
}  // namespace
#endif // !HAVE_ALSA


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
#ifndef HAVE_ALSA
	/* Native event-driven IPAudioPlus gain plus timer for start/stop only. */
	if (eApp)
	{
		m_ipaNativeTimer = eTimer::create(eApp);
		m_ipaNativeConnection = m_ipaNativeTimer->timeout.connect(
			sigc::mem_fun(*this, &eDVBVolumecontrol::ipaNativeTick));
		m_ipaNativeTimer->start(600, false);
		eDebug("[IPA Native Master V2] Watcher initialized (inactive until enabled flag)");
		if (ipaNativeEnabled() && ipaNativePlayerActive())
			ipaNativeSession.sync(muted ? 0 : leftVol, true);
	}
#endif
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
	/* V2: this MUST run before the DVB mixer branch: that branch can return
	 * early when /dev/dvb/adapter0/audio0 is available. */
#ifndef HAVE_ALSA
	if (ipaNativeEnabled() || ipaNativeSession.active)
		ipaNativeSession.sync(muted ? 0 : leftVol, ipaNativePlayerActive());
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
#ifndef HAVE_ALSA
	if (ipaNativeEnabled() || ipaNativeSession.active)
		ipaNativeSession.sync(0, ipaNativePlayerActive());
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
#ifndef HAVE_ALSA
	if (ipaNativeEnabled() || ipaNativeSession.active)
		ipaNativeSession.sync(leftVol, ipaNativePlayerActive());
#endif
}

void eDVBVolumecontrol::volumeToggleMute()
{
	if (isMuted())
		volumeUnMute();
	else
		volumeMute();
}

#ifndef HAVE_ALSA
void eDVBVolumecontrol::ipaNativeTick()
{
    /* The timer handles session startup/cleanup; volume changes happen
     * synchronously in setVolume, volumeMute and volumeUnMute. */
    static bool previouslyEnabled = false;
    const bool enabled = ipaNativeEnabled();
    if (enabled != previouslyEnabled)
    {
        eDebug("[IPA Native Master V2] %s", enabled ?
            "Enabled; checking for IPAudioPlus GStreamer" : "Disabled");
        previouslyEnabled = enabled;
    }
    if (!enabled)
    {
        ipaNativeSession.finish();
        return;
    }
    ipaNativeSession.sync(muted ? 0 : leftVol, ipaNativePlayerActive());
}
#endif
