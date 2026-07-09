/*
 * xiao-c6-slcan  —  CAN sniffer/gateway firmware for the Seeed XIAO ESP32-C6
 * ---------------------------------------------------------------------------
 * Speaks the SLCAN / LAWICEL ASCII protocol over the native USB-CDC serial
 * port, so it plugs straight into python-can:
 *
 *     can.Bus(interface="slcan", channel="/dev/cu.usbmodemXXXX",
 *             bitrate=500000, listen_only=True)
 *
 * It is also compatible with SavvyCAN / cangaroo / can-utils slcand.
 *
 * Hardware:  XIAO ESP32-C6 + SN65HVD230 ("VP230") 3.3 V CAN transceiver.
 *   TWAI TX  = GPIO21 (silkscreen D3)  -> transceiver TXD/D
 *   TWAI RX  = GPIO2  (silkscreen D2)  -> transceiver RXD/R
 *   (Either XIAO pin can be TX or RX via the GPIO matrix; if you wired them
 *    the other way and see NO frames, just swap PIN_CAN_TX / PIN_CAN_RX.)
 *
 * Safety:  the default open command from python-can with listen_only=True is
 * 'L' -> TWAI_MODE_LISTEN_ONLY, in which the controller transmits NOTHING
 * (no data, no ACK, no error frames) and cannot disturb a live vehicle bus.
 * Normal mode ('O') is only entered when the host explicitly asks for it
 * (e.g. to send OBD2 requests).
 *
 * NOTE: the ESP32-C6 TWAI peripheral is CLASSICAL CAN only. If a bus carries
 * CAN FD frames the controller reports them as bus errors — expected.
 */

#include <Arduino.h>
#include <string.h>
#include "driver/twai.h"

// ---------------------------------------------------------------------------
// Wiring / board config
// ---------------------------------------------------------------------------
#ifndef PIN_CAN_TX
#define PIN_CAN_TX GPIO_NUM_21   // silkscreen D3  -> transceiver TXD (D)
#endif
#ifndef PIN_CAN_RX
#define PIN_CAN_RX GPIO_NUM_2    // silkscreen D2  -> transceiver RXD (R)
#endif

#define LED_PIN 15               // on-board user LED (active-LOW)

static const uint32_t DEFAULT_BITRATE = 500000;   // Fiat 500 drivetrain bus

// Firmware version reported to `V` (2 decimal digits HW, 2 decimal digits SW).
#define FW_VERSION_STR "V1001"   // HW 1.0, SW 0.1

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
static uint32_t    g_bitrate    = DEFAULT_BITRATE;
static bool        g_open       = false;
static twai_mode_t g_mode       = TWAI_MODE_LISTEN_ONLY;
static bool        g_timestamps = false;     // slcan Zn; python-can never enables this
static uint32_t    g_frames     = 0;         // total RX frames streamed

static char        g_cmd[128];
static uint8_t     g_cmdlen = 0;

// Batched USB-CDC output buffer (frames). Command acks are written directly.
static char        g_out[6144];
static size_t      g_outlen = 0;

static char        g_serial4[5] = "0000";    // 4-char device serial for `N`

static const char  HEXD[] = "0123456789ABCDEF";   // NB: Arduino's Print.h #defines HEX=16
static const char  CR  = '\r';   // 0x0D  slcan OK / line terminator
static const char  BEL = '\a';   // 0x07  slcan error

// ---------------------------------------------------------------------------
// Output helpers
// ---------------------------------------------------------------------------
static inline void out_flush() {
  if (g_outlen) { Serial.write((const uint8_t *)g_out, g_outlen); g_outlen = 0; }
}
static inline void out_putc(char c) {
  if (g_outlen >= sizeof(g_out)) out_flush();
  g_out[g_outlen++] = c;
}
static inline void ack(char c) { Serial.write((uint8_t)c); }

// ---------------------------------------------------------------------------
// Hex helpers
// ---------------------------------------------------------------------------
static int hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}
static bool parse_hex(const char *s, int n, uint32_t *out) {
  uint32_t v = 0;
  for (int i = 0; i < n; i++) {
    int h = hexval(s[i]);
    if (h < 0) return false;
    v = (v << 4) | (uint32_t)h;
  }
  *out = v;
  return true;
}

// ---------------------------------------------------------------------------
// Bit-timing
// ---------------------------------------------------------------------------
// SLCAN Sn code -> bit/s, using python-can's mapping (note S7=750k, S8=1M,
// S9=83.3k — these differ from the classic LAWICEL table but match python-can).
static uint32_t bitrate_for_scode(char c) {
  switch (c) {
    case '0': return 10000;
    case '1': return 20000;
    case '2': return 50000;
    case '3': return 100000;
    case '4': return 125000;
    case '5': return 250000;
    case '6': return 500000;   // <-- Fiat 500 / most OBD2 CAN
    case '7': return 750000;   // no TWAI macro -> unsupported below
    case '8': return 1000000;
    case '9': return 83300;    // no TWAI macro -> unsupported below
    default:  return 0;
  }
}

static bool timing_for(uint32_t br, twai_timing_config_t *t) {
  switch (br) {
    case 1000000: { twai_timing_config_t c = TWAI_TIMING_CONFIG_1MBITS();   *t = c; return true; }
    case 800000:  { twai_timing_config_t c = TWAI_TIMING_CONFIG_800KBITS(); *t = c; return true; }
    case 500000:  { twai_timing_config_t c = TWAI_TIMING_CONFIG_500KBITS(); *t = c; return true; }
    case 250000:  { twai_timing_config_t c = TWAI_TIMING_CONFIG_250KBITS(); *t = c; return true; }
    case 125000:  { twai_timing_config_t c = TWAI_TIMING_CONFIG_125KBITS(); *t = c; return true; }
    case 100000:  { twai_timing_config_t c = TWAI_TIMING_CONFIG_100KBITS(); *t = c; return true; }
    case 50000:   { twai_timing_config_t c = TWAI_TIMING_CONFIG_50KBITS();  *t = c; return true; }
    case 25000:   { twai_timing_config_t c = TWAI_TIMING_CONFIG_25KBITS();  *t = c; return true; }
    case 20000:   { twai_timing_config_t c = TWAI_TIMING_CONFIG_20KBITS();  *t = c; return true; }
    default: return false;   // 10k/750k/83.3k not exposed as a TWAI macro here
  }
}

// ---------------------------------------------------------------------------
// Channel open/close
// ---------------------------------------------------------------------------
static void close_channel() {
  if (!g_open) return;
  twai_stop();
  twai_driver_uninstall();
  g_open = false;
}

static bool open_channel(twai_mode_t mode) {
  close_channel();

  twai_timing_config_t t_config;
  if (!timing_for(g_bitrate, &t_config)) return false;

  twai_general_config_t g_config =
      TWAI_GENERAL_CONFIG_DEFAULT(PIN_CAN_TX, PIN_CAN_RX, mode);
  g_config.rx_queue_len   = 64;    // absorb bursts on a busy 500k bus (default 5 drops frames)
  g_config.tx_queue_len   = 32;
  g_config.alerts_enabled = TWAI_ALERT_NONE;

  twai_filter_config_t f_config = TWAI_FILTER_CONFIG_ACCEPT_ALL();

  if (twai_driver_install(&g_config, &t_config, &f_config) != ESP_OK) return false;
  if (twai_start() != ESP_OK) { twai_driver_uninstall(); return false; }

  g_open = true;
  g_mode = mode;
  return true;
}

// ---------------------------------------------------------------------------
// Transmit (host -> bus), only valid when open in NORMAL mode
// ---------------------------------------------------------------------------
static bool cmd_transmit(const char *line, uint8_t len) {
  if (!g_open || g_mode != TWAI_MODE_NORMAL) return false;

  char c = line[0];
  bool ext = (c == 'T' || c == 'R');
  bool rtr = (c == 'r' || c == 'R');
  int  idlen = ext ? 8 : 3;

  if (len < (uint8_t)(1 + idlen + 1)) return false;

  uint32_t id;
  if (!parse_hex(line + 1, idlen, &id)) return false;

  int dc = line[1 + idlen] - '0';
  if (dc < 0 || dc > 8) return false;

  twai_message_t m;
  memset(&m, 0, sizeof(m));
  m.identifier       = id;
  m.extd             = ext ? 1 : 0;
  m.rtr              = rtr ? 1 : 0;
  m.data_length_code = dc;

  if (!rtr) {
    const char *d = line + 2 + idlen;
    if (len < (uint8_t)(2 + idlen + dc * 2)) return false;
    for (int i = 0; i < dc; i++) {
      int hi = hexval(d[i * 2]), lo = hexval(d[i * 2 + 1]);
      if (hi < 0 || lo < 0) return false;
      m.data[i] = (uint8_t)((hi << 4) | lo);
    }
  }

  return twai_transmit(&m, pdMS_TO_TICKS(20)) == ESP_OK;
}

// ---------------------------------------------------------------------------
// Status flags for `F`
// ---------------------------------------------------------------------------
static uint8_t status_flags() {
  twai_status_info_t s;
  if (twai_get_status_info(&s) != ESP_OK) return 0;
  uint8_t f = 0;
  if (s.msgs_to_rx >= 60) f |= 0x01;                                   // RX FIFO ~full
  if (s.msgs_to_tx >= 30) f |= 0x02;                                   // TX FIFO ~full
  if (s.rx_error_counter > 127 || s.tx_error_counter > 127) f |= 0x20; // error passive
  if (s.state == TWAI_STATE_BUS_OFF) f |= 0x80;                        // bus error / off
  return f;
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------
static void process_command(char *line, uint8_t len) {
  if (len == 0) return;                 // bare CR (e.g. python-can's empty data-bitrate write)
  char c = line[0];

  switch (c) {
    case 'S':                            // set bitrate (channel must be closed)
      if (len < 2 || g_open) { ack(BEL); break; }
      {
        uint32_t br = bitrate_for_scode(line[1]);
        twai_timing_config_t tmp;
        if (br && timing_for(br, &tmp)) { g_bitrate = br; ack(CR); }
        else ack(BEL);
      }
      break;

    case 's': ack(BEL); break;           // custom BTR0/BTR1 not supported by TWAI

    case 'O': ack(open_channel(TWAI_MODE_NORMAL)      ? CR : BEL); break;
    case 'L': ack(open_channel(TWAI_MODE_LISTEN_ONLY) ? CR : BEL); break;
    case 'C': close_channel(); ack(CR); break;

    case 't': case 'T': case 'r': case 'R':   // transmit (X0 default -> CR ack, not z/Z)
      ack(cmd_transmit(line, len) ? CR : BEL);
      break;

    case 'd': case 'D': case 'b': case 'B':   // CAN FD: not supported on C6 TWAI
      ack(BEL);
      break;

    case 'V':                            // version
      Serial.print(FW_VERSION_STR); ack(CR);
      break;

    case 'N':                            // serial number
      Serial.write('N'); Serial.print(g_serial4); ack(CR);
      break;

    case 'Z':                            // timestamp on/off
      if (len >= 2) { g_timestamps = (line[1] != '0'); ack(CR); } else ack(BEL);
      break;

    case 'F':                            // status flags
      if (!g_open) { ack(BEL); break; }
      {
        uint8_t f = status_flags();
        Serial.write('F'); Serial.write(HEXD[(f >> 4) & 0xF]); Serial.write(HEXD[f & 0xF]);
        ack(CR);
      }
      break;

    // Accepted and safely ignored (filters/auto-poll/uart-baud/startup)
    case 'M': case 'm': case 'X': case 'W': case 'U': case 'Q':
      ack(CR);
      break;

    default: ack(BEL); break;
  }
}

// ---------------------------------------------------------------------------
// Receive (bus -> host): drain the RX queue and stream SLCAN lines
// ---------------------------------------------------------------------------
static void drain_rx() {
  twai_message_t m;
  int budget = 1024;                     // cap per pass so serial input is still serviced
  while (budget-- > 0 && twai_receive(&m, 0) == ESP_OK) {
    int  idlen;
    char pfx;
    if (m.extd) { idlen = 8; pfx = m.rtr ? 'R' : 'T'; }
    else        { idlen = 3; pfx = m.rtr ? 'r' : 't'; }

    out_putc(pfx);
    for (int i = idlen - 1; i >= 0; i--)
      out_putc(HEXD[(m.identifier >> (i * 4)) & 0xF]);

    int dc = m.data_length_code;
    if (dc > 8) dc = 8;                   // classical CAN clamp
    out_putc((char)('0' + dc));

    if (!m.rtr) {
      for (int i = 0; i < dc; i++) {
        out_putc(HEXD[(m.data[i] >> 4) & 0xF]);
        out_putc(HEXD[m.data[i] & 0xF]);
      }
    }

    if (g_timestamps) {                   // 4 hex ms, wraps at 60000 (slcan Z1)
      uint16_t ts = (uint16_t)(millis() % 60000);
      for (int i = 3; i >= 0; i--) out_putc(HEXD[(ts >> (i * 4)) & 0xF]);
    }

    out_putc(CR);
    g_frames++;
  }
  out_flush();
}

// ---------------------------------------------------------------------------
// Periodic health + activity LED
// ---------------------------------------------------------------------------
static void service_health() {
  static uint32_t last = 0, last_frames = 0;
  uint32_t now = millis();
  if (now - last < 200) return;
  last = now;

  bool active = (g_frames != last_frames);
  last_frames = g_frames;
  digitalWrite(LED_PIN, (g_open && active) ? LOW : HIGH);   // LED on (LOW) when traffic flows

  // Bus-off recovery only matters in NORMAL mode (listen-only never transmits).
  if (g_open && g_mode == TWAI_MODE_NORMAL) {
    twai_status_info_t s;
    if (twai_get_status_info(&s) == ESP_OK) {
      if (s.state == TWAI_STATE_BUS_OFF)      twai_initiate_recovery();
      else if (s.state == TWAI_STATE_STOPPED) twai_start();   // resume after recovery
    }
  }
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------
void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);           // LED off

  Serial.setRxBufferSize(1024);
  Serial.setTxBufferSize(8192);
  Serial.begin(115200);                  // baud is nominal on native USB-CDC

  uint64_t mac = ESP.getEfuseMac();
  uint16_t s16 = (uint16_t)(mac & 0xFFFF);
  for (int i = 0; i < 4; i++) g_serial4[3 - i] = HEXD[(s16 >> (i * 4)) & 0xF];
  g_serial4[4] = 0;

  // Stay idle until the host opens the channel with 'O' (normal) or 'L' (listen-only).
}

void loop() {
  // 1) host commands
  while (Serial.available()) {
    int ch = Serial.read();
    if (ch < 0) break;
    if (ch == CR) { g_cmd[g_cmdlen] = 0; process_command(g_cmd, g_cmdlen); g_cmdlen = 0; }
    else if (ch == '\n') { /* ignore LF */ }
    else if (g_cmdlen < sizeof(g_cmd) - 1) { g_cmd[g_cmdlen++] = (char)ch; }
    else { g_cmdlen = 0; }               // overflow -> resync
  }

  // 2) bus -> host
  if (g_open) drain_rx();

  // 3) LED + recovery
  service_health();
}
