#ifndef __OPENBH_DREAMTWO_TIMESHIFT_GAP_H
#define __OPENBH_DREAMTWO_TIMESHIFT_GAP_H

/*
 * OpenBH DreamTwo timeshift playback: gap detection and monotonic accounting.
 * This code never touches the recording file and never rewrites PTS/PCR.
 * Only intended for eFilePushThread created with timeshift flag 1.
 * Portable C++11, no dependencies on Enigma2, suitable for standalone tests.
 */

#include <stdint.h>
#include <stddef.h>
#include <time.h>

class eTimeshiftGapClock
{
public:
	struct Decision {
		uint64_t hold_ms;
		uint64_t pts_jump_ms;
		uint64_t elapsed_ms;
		bool got_pts;
		uint64_t final_pts;
		int video_pid;
		Decision() : hold_ms(0), pts_jump_ms(0), elapsed_ms(0),
			got_pts(false), final_pts(0), video_pid(-1) {}
	};

	eTimeshiftGapClock() : m_valid(false), m_pid(-1), m_pts(0), m_delivered_ms(0) {}

	void reset() { m_valid = false; m_pid = -1; m_pts = 0; m_delivered_ms = 0; }

	static uint64_t monotonicMs()
	{
		struct timespec ts = {};
		clock_gettime(CLOCK_MONOTONIC, &ts);
		return (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
	}

	/* Preflight a complete 188-byte packet-aligned output block.
	 * The decision refers to the whole block, so an audio packet cannot be
	 * dispatched ahead of its following recovered video PES in the same block.
	 */
	Decision inspect(const unsigned char *data, size_t len, uint64_t now_ms,
		uint64_t min_gap_ms = 700, uint64_t max_hold_ms = 120000) const
	{
		Decision d;
		if (!data || len < 188 || len % 188)
			return d;
		bool valid = m_valid;
		int pid = m_pid;
		uint64_t old_pts = m_pts;
		uint64_t elapsed = (valid && now_ms > m_delivered_ms)
			? now_ms - m_delivered_ms : 0;
		/* Only a single wall-time interval can be deducted from the next gap.
		 * If a block contains multiple discontinuities, preserve all of them. */
		bool deducted = false;
		for (size_t off = 0; off + 188 <= len; off += 188)
		{
			const unsigned char *p = data + off;
			if (p[0] != 0x47 || (p[1] & 0x80) || !(p[1] & 0x40))
				continue; // bad TS, transport error, or not PES start
			if ((p[3] & 0xc0) != 0) // only decrypted PES; don't parse encrypted bytes
				continue;
			int ts_pid = ((p[1] & 0x1f) << 8) | p[2];
			if (ts_pid == 0x1fff)
				continue;
			int adaptation = (p[3] >> 4) & 3;
			if (!(adaptation & 1))
				continue;
			size_t pos = off + 4;
			if (adaptation & 2)
			{
				if (pos >= off + 188)
					continue;
				pos += (size_t)data[pos] + 1;
			}
			if (pos + 14 > off + 188)
				continue;
			const unsigned char *v = data + pos;
			if (v[0] || v[1] || v[2] != 1 || v[3] < 0xe0 || v[3] > 0xef)
				continue;
			if ((v[6] & 0xc0) != 0x80 || !(v[7] & 0x80) || v[8] < 5)
				continue;
			const unsigned char *q = v + 9;
			if ((q[0] & 0xf1) != 0x21 && (q[0] & 0xf1) != 0x31)
				continue;
			if (!(q[2] & 1) || !(q[4] & 1))
				continue;
			uint64_t pts = ((((uint64_t)(q[0] >> 1)) & 7) << 30)
				| ((uint64_t)q[1] << 22) | (((uint64_t)q[2] >> 1) << 15)
				| ((uint64_t)q[3] << 7) | ((uint64_t)q[4] >> 1);
			pts &= ((1ULL << 33) - 1);
			if (!valid || pid != ts_pid)
			{
				valid = true;
				pid = ts_pid;
				old_pts = pts;
				deducted = true; // no synthetic hold across PID changes
			}
			else
			{
				int64_t difference = (int64_t)((pts - old_pts) & ((1ULL << 33) - 1));
				if (difference >= (1LL << 32))
					difference -= (1LL << 33);
				if (difference < -90000) // significant reverse seek/epoch reset
				{
					old_pts = pts;
					deducted = true;
				}
				else if (difference > 0)
				{
					uint64_t jump_ms = ((uint64_t)difference * 1000) / 90000;
					/* Subtract real time passed since the last video PTS block.
					 * Prevents double freezing when playback was already waiting at EOF. */
					uint64_t used_wall = deducted ? 0 : elapsed;
					if (jump_ms > used_wall + min_gap_ms)
					{
						uint64_t hold_ms = jump_ms - used_wall;
						/* A huge discontinuity is more likely a new PCR/PTS epoch
						 * than missing reception. Do not freeze for minutes. */
						if (hold_ms <= max_hold_ms)
						{
							d.hold_ms += hold_ms;
							d.pts_jump_ms += jump_ms;
							d.elapsed_ms += used_wall;
						}
						deducted = true;
					}
					old_pts = pts;
				}
				/* Small negative PTS differences can be B-frame reordering;
				 * keep the most recent high-water mark. */
			}
			d.got_pts = true;
			d.final_pts = old_pts;
			d.video_pid = pid;
		}
		return d;
	}

	/* Call only after the complete block has been handed to the demux.
	 * This means the next decision uses time the demux already consumed. */
	void delivered(const Decision &d, uint64_t now_ms)
	{
		if (!d.got_pts)
			return;
		m_valid = true;
		m_pid = d.video_pid;
		m_pts = d.final_pts;
		m_delivered_ms = now_ms;
	}

private:
	bool m_valid;
	int m_pid;
	uint64_t m_pts;
	uint64_t m_delivered_ms;
};

#endif
