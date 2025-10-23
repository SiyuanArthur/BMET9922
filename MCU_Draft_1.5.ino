#include "BluetoothSerial.h"
#include <Esp.h>

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth is not enabled! Please run menuconfig to enable it
#endif

BluetoothSerial SerialBT;

// ----- Bluetooth Failsafe Setup -----
volatile bool isConnected = false;
String btName = "ESP32_PulseDevice";

// ----- Hardware Config -----
#define PULSE_SENSOR_PIN 25
#define BUTTON_PIN       33
#define LED_PIN          32
#define DEBOUNCE_COUNT   5
#define SAMPLE_PERIOD_MS 20 // 50 samples per second

// ----- Signal Variables -----
unsigned long lastSampleTime = 0;
unsigned long lastSendTime = 0;
int sampleBuffer[50];
int sampleIndex = 0;
bool buttonState = false;
bool lastButtonReading = false;
int debounceCounter = 0;
float bpm = 0;
int threshold = 0;

// For BPM calculation (simple threshold method)
unsigned long lastBeatTime = 0;
int beatCount = 0;

// ----- Bluetooth Callback -----
void btCallback(esp_spp_cb_event_t event, esp_spp_cb_param_t *param) {
  if (event == ESP_SPP_SRV_OPEN_EVT) {
    isConnected = true;
    Serial.println("Client Connected!");
    digitalWrite(LED_PIN, HIGH);
  } 
  else if (event == ESP_SPP_CLOSE_EVT) {
    isConnected = false;
    Serial.println("Client Disconnected!");
    digitalWrite(LED_PIN, LOW);
    // Optional restart on connection loss
    // SerialBT.end(); delay(100); SerialBT.begin(btName);
  } 
  else {
    Serial.print("BT Event: ");
    Serial.println(event);
  }
}

// ----- Setup -----
void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);

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

// ----- Signal Filtering -----
//const int filterSize = 5;
//int filterBuffer[filterSize];
//int filterIndex = 0;

//int getFilteredValue(int newVal) {
  //filterBuffer[filterIndex++] = newVal;
  //if (filterIndex >= filterSize) filterIndex = 0;

  //int sum = 0;
  //for (int i = 0; i < filterSize; i++) sum += filterBuffer[i];
  //return sum / filterSize;
//}

// ----- Main Loop -----
void loop() {
  unsigned long currTime = millis();

  // 1. Sample every 20 ms
  if (currTime - lastSampleTime >= SAMPLE_PERIOD_MS) {
    lastSampleTime = currTime;

    int sensorValue = analogRead(PULSE_SENSOR_PIN);

    //int rawValue = analogRead(PULSE_SENSOR_PIN);
    //int sensorValue = getFilteredValue(rawValue);

    sampleBuffer[sampleIndex++] = sensorValue;

    // Button Debounce
    bool currentReading = !digitalRead(BUTTON_PIN);
    if (currentReading != lastButtonReading) {
      debounceCounter = 0;
    } else if (debounceCounter < DEBOUNCE_COUNT) {
      debounceCounter++;
      if (debounceCounter >= DEBOUNCE_COUNT)
        buttonState = currentReading;
    }
    lastButtonReading = currentReading;

    // Adaptive Threshold Detection
    static int runningMin = 1024, runningMax = 0;
    if (sensorValue < runningMin) runningMin = sensorValue;
    if (sensorValue > runningMax) runningMax = sensorValue;
    threshold = (runningMin + runningMax) / 2;

    // Rising Edge Detection for BPM
    static bool lastAbove = false;
    bool aboveThreshold = (sensorValue > threshold);

    if (!lastAbove && aboveThreshold) {
      unsigned long beatInterval = currTime - lastBeatTime;
      if (beatInterval > 250) { // Cap at 240 BPM
        bpm = 60000.0 / beatInterval;
        lastBeatTime = currTime;
        beatCount++;
        digitalWrite(LED_PIN, HIGH);
      }
    } else {
      digitalWrite(LED_PIN, LOW);
    }
    lastAbove = aboveThreshold;

    // Visualization Output
    Serial.print(sensorValue);
    Serial.print(",");
    Serial.println(threshold);

    // Send packet over Bluetooth every second
    if (sampleIndex >= 50) {
      if (isConnected) {
        sendSerialPacket();
      } else {
        Serial.println("Waiting for Bluetooth connection...");
      }
      sampleIndex = 0;
      runningMin = 1024;
      runningMax = 0;
      beatCount = 0;
    }
  }
}

// ----- Data Packet Sender -----
void sendSerialPacket() {
  Serial.print("BPM:");
  Serial.print(bpm, 1);
  Serial.print(" Data:");
  for (int i = 0; i < 50; i++) {
    Serial.print(sampleBuffer[i]);
    if (i < 49) Serial.print(",");
  }
  Serial.print(" Threshold:");
  Serial.print(threshold);
  Serial.print(" Button:");
  Serial.println(buttonState ? "1" : "0");

  // Send same packet via Bluetooth
  if (isConnected) {
    SerialBT.printf("BPM:%.1f Data:", bpm);
    for (int i = 0; i < 50; i++) {
      SerialBT.printf("%d,", sampleBuffer[i]);
    }
    SerialBT.printf(" Threshold:%d Button:%d\n", threshold, buttonState);
  }
}

