#include "BluetoothSerial.h"
#include <Esp.h>
#include "switch.h"

BluetoothSerial SerialBT;

// --- Hardware config ---
#define PULSE_SENSOR_PIN  26
#define BUTTON_PIN        35
#define LED_BT            33  // Bluetooth status LED (ensure it's output-capable on your board)
#define LED_REC           32  // Recording status LED (output-capable pin)
#define SAMPLE_PERIOD_MS  20  // 50 samples/s

// --- Logic variables ---
volatile bool isConnected = false;
String btName = "ESP32_PulseDevice";
bool recording = false;

unsigned long lastSampleTime = 0;
int sampleBuffer[200];
int sampleIndex = 0;
float bpm = 0;
int threshold = 0;
unsigned long lastBeatTime = 0;
int beatCount = 0;

// --- Debounced Switch for Recording Button ---
Switch recordBtn(0, 5); // id = 0, threshold = 5 (tune for your button/materials)

// --- LED timers (if you want BT LED to blink when not connected) ---
unsigned long ledBtTimer = 0;
const unsigned long ledBtBlinkInterval = 500;

// --- Bluetooth callback ---
void btCallback(esp_spp_cb_event_t event, esp_spp_cb_param_t *param) {
  if (event == ESP_SPP_SRV_OPEN_EVT) {
    isConnected = true;
    Serial.println("Client Connected!");
  } 
  else if (event == ESP_SPP_CLOSE_EVT) {
    isConnected = false;
    Serial.println("Client Disconnected!");
  } 
  else {
    Serial.print("BT Event: ");
    Serial.println(event);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_BT, OUTPUT);
  pinMode(LED_REC, OUTPUT);

  // Set ADC attenuation for pulse sensor
  analogSetAttenuation(ADC_11db);

  SerialBT.register_callback(btCallback);
  SerialBT.begin(btName);

  Serial.println("PPG,Threshold");
}

void loop() {
  unsigned long currTime = millis();

  // --- Sample pulse sensor every 20ms ---
  if (currTime - lastSampleTime >= SAMPLE_PERIOD_MS) {
    lastSampleTime = currTime;

    int sensorValue = analogRead(PULSE_SENSOR_PIN); // NO FILTERING
    sampleBuffer[sampleIndex++] = sensorValue;

    // Use Switch class for button debounce and "edge-detect" logic
    bool physicalBtn = !digitalRead(BUTTON_PIN); // Active LOW
    if (recordBtn.update(physicalBtn) && recordBtn.state()) {
      // Button transitioned (pressed after stable threshold)
      recording = !recording;
      Serial.printf("Recording:%d\n", recording ? 1 : 0);
      SerialBT.printf("Recording:%d\n", recording ? 1 : 0);
    }

    // Adaptive threshold (min/max adaptation)
    static int runningMin = 4095, runningMax = 0;
    if (sensorValue < runningMin) runningMin = sensorValue;
    if (sensorValue > runningMax) runningMax = sensorValue;
    threshold = (runningMin * 0.4 + runningMax * 0.6);

    // Pulse detection (rising edge)
    static bool lastAbove = false;
    bool aboveThreshold = (sensorValue > threshold);
    if (!lastAbove && aboveThreshold) {
      unsigned long beatInterval = currTime - lastBeatTime;
      if (beatInterval > 250) {
        bpm = 60000.0 / beatInterval;
        lastBeatTime = currTime;
        beatCount++;
      }
    }
    lastAbove = aboveThreshold;

    // Serial plot output
    Serial.print(sensorValue);
    Serial.print(",");
    Serial.println(threshold);

    // Send buffer every 200 samples
    if (sampleIndex >= 200) {
      if (isConnected) sendSerialPacket(200);
      else Serial.println("Waiting for Bluetooth connection...");

      sampleIndex = 0;
      runningMin = 4095;
      runningMax = 0;
      beatCount = 0;
    }
  }

  // --- Bluetooth status LED ---
  if (isConnected) digitalWrite(LED_BT, HIGH);
  else if (millis() - ledBtTimer >= ledBtBlinkInterval) {
    ledBtTimer = millis();
    digitalWrite(LED_BT, !digitalRead(LED_BT));
  }

  // --- REC LED logic (only FULL ON when recording active) ---
  digitalWrite(LED_REC, recording ? LOW : HIGH);
}

void sendSerialPacket(int bufsize) {
  Serial.print("BPM:");
  Serial.print(bpm, 1);
  Serial.print(" Data:");
  for (int i = 0; i < bufsize; i++) {
    Serial.print(sampleBuffer[i]);
    if (i < bufsize - 1) Serial.print(",");
  }
  Serial.print(" Threshold:");
  Serial.print(threshold);
  Serial.print(" Button:");
  Serial.println(recording ? "1" : "0");

  if (isConnected) {
    SerialBT.printf("BPM:%.1f Data:", bpm);
    for (int i = 0; i < bufsize; i++) {
      SerialBT.printf("%d,", sampleBuffer[i]);
    }
    SerialBT.printf(" Threshold:%d Button:%d\n", threshold, recording ? 1 : 0);
  }
}

