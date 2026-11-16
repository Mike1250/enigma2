/* DreamTwo / DreamOne native dreamaudiod ALSA Master bridge.
 * This is built only when DREAMNEXTGEN is enabled. The original DVB ioctl
 * volume path remains entirely untouched for non-DreamNextGen receivers.
 * No compile-time ALSA dependency: libasound.so.2 is loaded at runtime.
 */
#include <enigma2_config.h>
#include <dlfcn.h>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <lib/dvb/dreamaudio_volume.h>

namespace {
class MasterMixer {
    void *library = nullptr;
    void *handle = nullptr;
    void *master = nullptr;
    long min_value = 0, max_value = 0;
    int (*mixer_open)(void **, int) = nullptr;
    int (*mixer_attach)(void *, const char *) = nullptr;
    int (*selem_register)(void *, void *, void *) = nullptr;
    int (*mixer_load)(void *) = nullptr;
    int (*mixer_close)(void *) = nullptr;
    void *(*first_elem)(void *) = nullptr;
    void *(*next_elem)(void *) = nullptr;
    const char *(*elem_name)(void *) = nullptr;
    unsigned int (*elem_index)(void *) = nullptr;
    int (*has_volume)(void *) = nullptr;
    int (*get_range)(void *, long *, long *) = nullptr;
    int (*set_volume_all)(void *, long) = nullptr;

    void teardown() {
        master = nullptr;
        if (handle && mixer_close) mixer_close(handle);
        handle = nullptr;
        if (library) dlclose(library);
        library = nullptr;
    }

    template<typename T> bool symbol(T &slot, const char *name) {
        slot = reinterpret_cast<T>(dlsym(library, name));
        return slot != nullptr;
    }

    bool connect() {
        if (master) return true;
        if (!library) {
            library = dlopen("libasound.so.2", RTLD_NOW | RTLD_LOCAL);
            if (!library) return false;
            if (!symbol(mixer_open, "snd_mixer_open") ||
                !symbol(mixer_attach, "snd_mixer_attach") ||
                !symbol(selem_register, "snd_mixer_selem_register") ||
                !symbol(mixer_load, "snd_mixer_load") ||
                !symbol(mixer_close, "snd_mixer_close") ||
                !symbol(first_elem, "snd_mixer_first_elem") ||
                !symbol(next_elem, "snd_mixer_elem_next") ||
                !symbol(elem_name, "snd_mixer_selem_get_name") ||
                !symbol(elem_index, "snd_mixer_selem_get_index") ||
                !symbol(has_volume, "snd_mixer_selem_has_playback_volume") ||
                !symbol(get_range, "snd_mixer_selem_get_playback_volume_range") ||
                !symbol(set_volume_all, "snd_mixer_selem_set_playback_volume_all")) {
                teardown();
                return false;
            }
        }
        if (mixer_open(&handle, 0) < 0 ||
            mixer_attach(handle, "hw:0") < 0 ||
            selem_register(handle, nullptr, nullptr) < 0 ||
            mixer_load(handle) < 0) {
            teardown();
            return false;
        }
        for (void *e = first_elem(handle); e; e = next_elem(e)) {
            const char *name = elem_name(e);
            if (name && std::strcmp(name, "Master") == 0 &&
                elem_index(e) == 0 && has_volume(e)) {
                master = e;
                break;
            }
        }
        if (!master || get_range(master, &min_value, &max_value) < 0 ||
            min_value >= max_value) {
            teardown();
            return false;
        }
        std::fprintf(stderr, "[DreamAudio Master] attached hw:0 Master range=%ld..%ld\n",
                     min_value, max_value);
        return true;
    }

public:
    ~MasterMixer() { teardown(); }
    bool set(unsigned int ui) {
        if (!connect()) return false;
        if (ui > 100) ui = 100;
        long raw;
        if (!ui)
            raw = min_value;
        else if (ui >= 100)
            raw = max_value;
        else if (min_value == 0 && max_value == 255) {
            /* Inverse of dreamaudiod.cpp Output::gain():
             * gain = 10^((-51 + 0.2*raw)/20).
             * UI 50% -> approx raw 225, UI 100% -> 255.
             */
            raw = std::lround(255.0 + 100.0 * std::log10(ui / 100.0));
            raw = std::max(min_value, std::min(max_value, raw));
        } else {
            /* Unexpected ALSA Master range; preserve functioning volume
             * instead of applying the 0..255-specific DreamAudio curve. */
            raw = min_value + (max_value - min_value) * ui / 100;
        }
        if (set_volume_all(master, raw) < 0) {
            std::fprintf(stderr, "[DreamAudio Master] could not set level=%u raw=%ld\n", ui, raw);
            teardown();
            return false;
        }
        return true;
    }
};
} // namespace

void dreamaudioApplyMaster(unsigned int ui)
{
    static MasterMixer mixer;
    static bool warned = false;
    if (!mixer.set(ui)) {
        if (!warned) {
            std::fprintf(stderr, "[DreamAudio Master] ALSA hw:0 Master is unavailable; check amixer -c 0 sget Master\n");
            warned = true;
        }
    } else {
        warned = false;
    }
}
