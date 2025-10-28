/*
 * BMET2922 Wearable PPG – ESP32 Bluetooth SPP sender (Final, Min/Max Threshold for Plotter)
 *   - Serial plotter output: raw + min/max threshold (for visual adaptation)
 *   - Advanced filtering/EMA+std threshold for beat detection/BPM (unchanged)
 *   - Bluetooth NDJSON packets for BMET2922 Python GUI
 *   - BT LED blinks when disconnected, solid when connected
 *   - REC LED on only when recording
 *   - Button toggles recording (debounced with 5 stable counts)
 */
#include <Arduino.h>                           // Include Arduino core functions for ESP32
#include "BluetoothSerial.h"                   // Include Bluetooth Serial library for ESP32

BluetoothSerial SerialBT;                     // Instantiate Bluetooth Serial object

// ============ Hardware pins ============
#define PIN_ADC     26                        // GPIO26, analog input connected to pulse sensor output
#define PIN_BTN     35                        // GPIO35, push-button input (active LOW, pulled-up internally)
#define PIN_LED_BT  33                        // GPIO33, LED indicating Bluetooth connection status
#define PIN_LED_REC 32                        // GPIO32, LED indicating recording status

#define DEVICE_NAME "ESP32_PPG"               // Bluetooth device name advertised to host devices

// ========== Signal processing config ==========
#define SAMPLE_HZ        50                    // Sampling frequency in Hz (50 samples/second)
#define PKT_HZ           1                     // Full data packet transmission rate in Hz (1 packet/second)
#define BPM_BROADCAST_HZ 5                     // BPM-only update rate in Hz (5 updates/second)
#define MIN_PEAK_INTERVAL 300                  // Minimum allowable peak interval in ms (~200 bpm max from exercise or stress-induced rates)
#define MAX_PEAK_INTERVAL 2000                 // Maximum allowable peak interval in ms (~30 bpm min from bradycardia)
#define HP_CUTOFF_HZ     0.3f                  // High-pass filter cutoff frequency in Hz for baseline wander removal
#define LP_CUTOFF_HZ     5.0f                  // Low-pass filter cutoff frequency in Hz for noise reduction
#define EMA_WINDOW_SEC   5.0f                  // Length of EMA (exponential moving average) window in seconds
#define THR_K            1.0f                  // Scaling factor for adaptive threshold calculation
#define LOW_AMP_THRESH   40                    // Threshold for detecting low amplitude signal (flagging)

// ============ Button Debounce variables =========
static uint32_t lastDebounceSample = 0;
const int DEBOUNCE_INTERVAL_MS = 20; 

// ========== Buffers and state variables ==========
static char jsonBuf[640], tmpBuf[48];          // Buffers for composing JSON strings to send over Bluetooth
volatile uint16_t seq = 0;                      // Sequence number incremented for each full data packet
uint32_t lastSampleMs = 0, lastPktMs = 0, lastBpmMs = 0; // Timing markers for sampling and transmissions
int samples[50], sampIdx = 0;                   // Circular buffer storing raw samples collected in last second

// Filtering internal state variables
float dt_s = 1.0f / SAMPLE_HZ;                  // Sampling interval duration in seconds
float a_hp = 0.0f, hp_y_prev = 0.0f, hp_x_prev = 0.0f;    // High-pass filter smoothing factor and history variables
float a_lp = 0.0f, lp_y_prev = 0.0f;             // Low-pass filter smoothing factor and previous filtered value

// EMA state for adaptive thresholding of filtered signal
float emaMean = NAN, emaVar = 0.0f, emaAlpha = 0.0f;

// Beat detection variables for peak tracking
float filtPrev = 0.0f; bool aboveThr = false;    // Last filtered sample and whether signal is currently above threshold
float candPeak = 0.0f; uint32_t candStartMs = 0, lastPeakMs = 0; // Candidate peak amplitude and timing, last beat occurrence time
float filtMin = 1e9f, filtMax = -1e9f;           // Track min/max filtered signal amplitude for amplitude check

// Button state and debounce variables (with 5-stable count debounce logic)
bool recording = false;                           // Flag indicating whether recording mode is active
static int stableCount = 0;                       // Counter for consecutive stable button reads
static int lastReading = HIGH;                    // Last raw digital reading of button input
static int debouncedState = HIGH;                 // Current debounced button state

// For visual adaptive threshold via min/max on raw samples
static int runningMin = 4095, runningMax = 0;     // Min and max raw ADC values over recent samples for threshold visualization

// --- Helper Functions ---

// Read raw analog sensor value from specified ADC pin
static inline int read_ppg() { 
  return analogRead(PIN_ADC); 
}

// Send JSON string line via Bluetooth serial port ending with newline
static inline void bt_write_line(const char* s) {
  SerialBT.write((const uint8_t*)s, strlen(s));  // Send JSON text bytes
  SerialBT.write('\n');                           // Send newline character as delimiter
}

// Update EMA mean and variance using current filtered value x
static inline void ema_update(float x) {
  if (isnan(emaMean)) {                           // Initialize EMA on first call
    emaMean = x; 
    emaVar = 0.0f; 
    return; 
  }
  float d = x - emaMean;                          // Difference from current mean
  emaMean += emaAlpha * d;                        // Update mean with smoothing factor
  emaVar = (1.0f - emaAlpha) * emaVar + emaAlpha * d * d;  // Update variance accordingly
}

// --- Setup Function ---

void setup() {
  Serial.begin(115200);                           // Open USB serial for debug and serial plotter output
  analogReadResolution(12);                       // Setup ADC to use 12-bit resolution (0-4095)
  analogSetAttenuation(ADC_11db);                 // Set analog input attenuation to support wider voltage range

  pinMode(PIN_BTN, INPUT_PULLUP);                 // Configure pushbutton input as input with internal pull-up resistor
  pinMode(PIN_LED_BT, OUTPUT);                     // Bluetooth status LED as output
  pinMode(PIN_LED_REC, OUTPUT);                    // Recording status LED as output
  digitalWrite(PIN_LED_BT, LOW);                   // Initialize Bluetooth LED to OFF
  digitalWrite(PIN_LED_REC, LOW);                  // Initialize recording LED to OFF

  // Calculate and set filter smoothing coefficients using cutoff frequencies
  float RC_hp = 1.0f / (2.0f * PI * HP_CUTOFF_HZ); // Time constant for high-pass
  a_hp = RC_hp / (RC_hp + dt_s);                    // HP filter smoothing factor

  float RC_lp = 1.0f / (2.0f * PI * LP_CUTOFF_HZ); // Time constant for low-pass
  a_lp = RC_lp / (RC_lp + dt_s);                    // LP filter smoothing factor

  int windowN = (int)(EMA_WINDOW_SEC * SAMPLE_HZ);  // Compute EMA window size in samples
  if (windowN < 1) windowN = 1;                      // Ensure minimum window size of 1
  emaAlpha = 2.0f / (windowN + 1);                   // Compute EMA smoothing alpha factor

  if (!SerialBT.begin(DEVICE_NAME))                   // Initialize Bluetooth serial with device name
    Serial.println("BT failed to connect.");          // Print error if failed
  else
    Serial.print("BT ready: "), Serial.println(DEVICE_NAME);  // Print success message

  lastSampleMs = lastPktMs = lastBpmMs = millis();   // Initialize timing variables for sampling and packet sending
  randomSeed(esp_random());                           // Initialize random seed (used internally if needed)
}

// --- Main Loop Function ---

void loop() {
  uint32_t now = millis();                            // Get current time in milliseconds

  // --- Button debounce and recording toggle logic (count-based) ---
  if (millis() - lastDebounceSample >= DEBOUNCE_INTERVAL_MS) {
    lastDebounceSample = millis();

    int currReading = digitalRead(PIN_BTN);
    if (currReading == lastReading) {
        stableCount++;
    } else {
        stableCount = 0;
    }
    lastReading = currReading;

    if (stableCount >= 5 && currReading != debouncedState) {
        debouncedState = currReading;
        if (debouncedState == LOW) {
            recording = !recording;
        }
    }
  }


  // --- Bluetooth LED display logic ---
  if (SerialBT.hasClient()) {                           // If Bluetooth host is connected
    digitalWrite(PIN_LED_BT, HIGH);                     // Turn Bluetooth LED ON solid
  } else {                                              // Otherwise
    static uint32_t lastBlink = 0;
    static bool blinkState = false;
    if (millis() - lastBlink > 500) {                   // Blink LED every 500 ms
      lastBlink = millis();
      blinkState = !blinkState;
      digitalWrite(PIN_LED_BT, blinkState ? HIGH : LOW);
    }
  }

  // --- Recording LED display logic ---
  digitalWrite(PIN_LED_REC, recording ? HIGH : LOW);    // Turn recording LED ON if recording, OFF otherwise (active low LED)

  // --- Signal sampling and processing ---
  if (now - lastSampleMs >= (1000 / SAMPLE_HZ)) {       // Time for next sample based on sample frequency
    lastSampleMs += (1000 / SAMPLE_HZ);                  // Schedule next sample timestamp

    int vRaw = read_ppg();                               // Read raw analog sensor value
    samples[sampIdx] = vRaw;                             // Store sample into circular buffer
    sampIdx = (sampIdx + 1) % 50;                        // Update sample buffer index with wrap-around

    // Apply bandpass filtering (cascade of HP then LP)
    float x = (float)vRaw;                               // Cast raw integer to float for digital filter math
    float hp_y = a_hp * (hp_y_prev + x - hp_x_prev);    // High-pass filter output
    hp_x_prev = x; hp_y_prev = hp_y;                     // Store state for next filter cycle
    float y = a_lp * lp_y_prev + (1.0f - a_lp) * hp_y;  // Low-pass filter output
    lp_y_prev = y;                                       // Save LP filter state

    // Track filtered amplitude for quality check
    if (y < filtMin) filtMin = y;
    if (y > filtMax) filtMax = y;

    // Calculate adaptive threshold for serial plotter visualization only (min/max threshold)
    if (vRaw < runningMin) runningMin = vRaw;
    if (vRaw > runningMax) runningMax = vRaw;
    int thresholdRaw = (int)(runningMin * 0.4 + runningMax * 0.6);

    // Output raw sample and threshold to serial plotter (CSV format)
    Serial.print(vRaw);
    Serial.print(",");
    Serial.println(thresholdRaw);

    // Reset running min/max after one second (50 samples)
    if (sampIdx == 0) {
      runningMin = 4095;
      runningMax = 0;
    }

    // Compute adaptive threshold based on EMA mean and stddev for peak detection and BPM calculation
    ema_update(y);
    float stdE = sqrtf(max(emaVar, 1e-6f));
    float thr = emaMean + THR_K * stdE;

    // Detect pulse peaks via threshold crossing and slope criteria
    float dv = y - filtPrev;
    if (!aboveThr && (y >= thr) && (dv > 0)) {
      aboveThr = true;
      candPeak = y;
      candStartMs = now;
    } else if (aboveThr) {
      if (y > candPeak) candPeak = y;
      if ((dv <= 0) || (now - candStartMs > MAX_PEAK_INTERVAL)) {
        uint32_t dt = now - lastPeakMs;
        if (dt > MIN_PEAK_INTERVAL && dt < MAX_PEAK_INTERVAL) {
          float currBpm = 60000.0f / dt;
          static float bpmLPF = NAN;
          if (isnan(bpmLPF)) bpmLPF = currBpm;
          else bpmLPF = bpmLPF + 0.3f * (currBpm - bpmLPF);
          lastPeakMs = now;
        }
        aboveThr = false;
      }
    }
    filtPrev = y;
  }

  // --- Update filtered BPM for broadcasting ---
  static float bpmInst = NAN, bpmLPF = NAN;
  {
    static uint32_t lastSeenPeak = 0, prevPeak = 0;
    if (lastPeakMs != 0 && lastPeakMs != lastSeenPeak) {
      if (prevPeak != 0) {
        uint32_t dt = lastPeakMs - prevPeak;
        if (dt > MIN_PEAK_INTERVAL && dt < MAX_PEAK_INTERVAL) {
          float currBpm = 60000.0f / dt;
          bpmInst = currBpm;
          if (isnan(bpmLPF)) bpmLPF = currBpm;
          else bpmLPF = bpmLPF + 0.3f * (currBpm - bpmLPF);
        }
      }
      prevPeak = lastPeakMs;
      lastSeenPeak = lastPeakMs;
    } else if (lastPeakMs && (millis() - lastPeakMs > MAX_PEAK_INTERVAL)) {
      bpmInst = NAN;
    }
  }

  // --- Send simple BPM JSON packet at 5 Hz to Bluetooth ---
  if (recording && (now - lastBpmMs >= (1000 / BPM_BROADCAST_HZ))) {
    lastBpmMs += (1000 / BPM_BROADCAST_HZ);
    float outBpm = isnan(bpmLPF) ? 0.0f : bpmLPF;
    int n = snprintf(jsonBuf, sizeof(jsonBuf), "{\"bpm\":%.1f}", outBpm);
    if (n > 0 && n < (int)sizeof(jsonBuf))
      bt_write_line(jsonBuf);
  }

  // --- Send full data packet at 1 Hz with samples and status flags ---
  if (recording && (now - lastPktMs >= (1000 / PKT_HZ))) {
    lastPktMs += (1000 / PKT_HZ);
    uint16_t flags = 0;
    if (debouncedState == LOW) flags |= 0x01;           // Button pressed flag
    if ((filtMax - filtMin) < LOW_AMP_THRESH) flags |= 0x02; // Low amplitude signal flag
    if (recording) flags |= 0x04;                        // Recording flag

    float outBpm = isnan(bpmLPF) ? 0.0f : bpmLPF;    //checks if the BPM is not a number and sets outBPM to 0 accordingly
    int written = snprintf(jsonBuf, sizeof(jsonBuf), "{\"bpm\":%.1f,\"samples\":[", outBpm);
    if (written <= 0 || written >= (int)sizeof(jsonBuf)) return;       // composes the JSON formatted string into a character buffer jsonbuf, forming the BT packet

    int start = sampIdx;
    for (int i = 0; i < 50; ++i) {      //Include last sample 50 samples in JSON array
      int idx = (start + i) % 50;
      int v = samples[idx];
      int n = snprintf(tmpBuf, sizeof(tmpBuf), (i == 49) ? "%d" : "%d,", v);
      if (written + n + 64 >= (int)sizeof(jsonBuf)) break;     // Ensures buffer has space
      memcpy(jsonBuf + written, tmpBuf, n);
      written += n;
    }
    int n = snprintf(jsonBuf + written, sizeof(jsonBuf) - written, "],\"seq\":%u,\"flags\":%u,\"t_mcu\":%lu}", seq, flags, (unsigned long)now);
    if (n > 0) {
      bt_write_line(jsonBuf);           // Send full packet to BT
      seq++;                            // Increment packet sequence
    }
    filtMin = 1e9f; filtMax = -1e9f;    // Reset min/max for next interval
  }
}
