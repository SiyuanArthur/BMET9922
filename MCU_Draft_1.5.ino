#include "BluetoothSerial.h"
#include <Esp.h>

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth is not enabled! Please run menuconfig to enable it
#endif

BluetoothSerial SerialBT;

// Hardware config
#define PULSE_SENSOR_PIN  25
#define BUTTON_PIN        33
#define LED_BT            32   // Bluetooth status LED
#define LED_REC           27   // Recording status LED
#define DEBOUNCE_COUNT    5
#define SAMPLE_PERIOD_MS  20   // 50 samples/s

// Signal variables
unsigned long lastSampleTime = 0;
int sampleBuffer[200];        // Buffer for min/max adaptation
int sampleIndex = 0;
bool buttonState = false;
bool lastButtonReading = false;
int debounceCounter = 0;
float bpm = 0;
int threshold = 0;
unsigned long lastBeatTime = 0;
int beatCount = 0;

// Bluetooth and logic
volatile bool isConnected = false;
String btName = "ESP32_PulseDevice";
bool recording = false;

// LED blink timers
unsigned long ledBtTimer = 0;
const unsigned long ledBtBlinkInterval = 500;
unsigned long ledRecTimer = 0;
const unsigned long ledRecBlinkInterval = 200;

// Bluetooth callback
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

  // Set ADC attenuation for best full-scale accuracy (recommended for ESP32)
  analogSetAttenuation(ADC_11db);

  Serial.println("Initializing Bluetooth...");
  SerialBT.register_callback(btCallback);
  if (!SerialBT.begin(btName)) {
    Serial.println("Error initializing Bluetooth!");
  } else {
    Serial.print("Bluetooth initialized as: ");
    Serial.println(btName);
  }

  Serial.println("PPG,Threshold");
}

void loop() {
  unsigned long currTime = millis();

  // 1. Sample every 20ms
  if (currTime - lastSampleTime >= SAMPLE_PERIOD_MS) {
    lastSampleTime = currTime;

    int sensorValue = analogRead(PULSE_SENSOR_PIN); // NO FILTERING
    sampleBuffer[sampleIndex++] = sensorValue;

    // Button debounce and edge detection for toggling recording
    bool currentReading = !digitalRead(BUTTON_PIN); // Active LOW
    if (currentReading != lastButtonReading) {
      debounceCounter = 0;
    } else if (debounceCounter < DEBOUNCE_COUNT) {
      debounceCounter++;
      if (debounceCounter >= DEBOUNCE_COUNT && currentReading && !buttonState) {
        recording = !recording;
        Serial.printf("Recording:%d\n", recording ? 1 : 0);
        SerialBT.printf("Recording:%d\n", recording ? 1 : 0);
        buttonState = true;
      }
    }
    if (!currentReading) buttonState = false;
    lastButtonReading = currentReading;

    // Adaptive threshold using buffer period longer for robustness
    static int runningMin = 4095, runningMax = 0;
    if (sensorValue < runningMin) runningMin = sensorValue;
    if (sensorValue > runningMax) runningMax = sensorValue;

    threshold = (runningMin * 0.4 + runningMax * 0.6);

    // Rising edge beat detection
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

    // Data packet every ~4s (200 samples) for smarter min/max adaptation
    if (sampleIndex >= 200) {
      if (isConnected) {
        sendSerialPacket(200);
      } else {
        Serial.println("Waiting for Bluetooth connection...");
      }
      sampleIndex = 0;
      runningMin = 4095;
      runningMax = 0;
      beatCount = 0;
    }
  }

  // Bluetooth status LED
  if (isConnected) {
    digitalWrite(LED_BT, HIGH);
  } else {
    if (millis() - ledBtTimer >= ledBtBlinkInterval) {
      ledBtTimer = millis();
      digitalWrite(LED_BT, !digitalRead(LED_BT));
    }
  }

  // Recording status LED
  if (recording) {
    digitalWrite(LED_REC, HIGH);
  } else {
    if (millis() - ledRecTimer >= ledRecBlinkInterval) {
      ledRecTimer = millis();
      digitalWrite(LED_REC, !digitalRead(LED_REC));
    }
  }
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

