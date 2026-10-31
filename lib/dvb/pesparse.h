#ifndef __lib_dvb_pesparse_h
#define __lib_dvb_pesparse_h

#include <asm/types.h>

class ePESParser
{
public:
	ePESParser();
	void setStreamID(unsigned char id, unsigned char id_mask=0xff);
	// Clear an incomplete PES frame when changing subtitle PID.
	void resetParser() { m_pes_position = 0; m_pes_length = 0; }
	void processData(const uint8_t *data, int len);
	virtual void processPESPacket(uint8_t *pkt, int len) = 0;
	virtual ~ePESParser() { }
private:
	unsigned char m_pes_buffer[65536+6];  // max pes packetlength + pes header
	int m_pes_position, m_pes_length;
	unsigned char m_header[4];
	unsigned char m_stream_id_mask;
};

#endif
