// Minimal Arduino API stub so generated sketches pass `g++ -fsyntax-only`.
// Declarations only; nothing here is linked or run.
#pragma once
#include <cstdint>
#include <cstddef>
#include <cmath>
#include <cstring>
#include <string>

typedef uint8_t byte;
typedef bool boolean;

#define HIGH 0x1
#define LOW 0x0
#define INPUT 0x01
#define OUTPUT 0x03
#define INPUT_PULLUP 0x05
#define INPUT_PULLDOWN 0x09
#define LED_BUILTIN 2
#define RISING 0x01
#define FALLING 0x02
#define CHANGE 0x03
#define A0 36

void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t val);
int digitalRead(uint8_t pin);
int analogRead(uint8_t pin);
void analogWrite(uint8_t pin, int val);
void analogReadResolution(uint8_t bits);
void delay(unsigned long ms);
void delayMicroseconds(unsigned int us);
unsigned long millis();
unsigned long micros();
unsigned long pulseIn(uint8_t pin, uint8_t state, unsigned long timeout = 1000000UL);
void attachInterrupt(uint8_t pin, void (*isr)(), int mode);
void detachInterrupt(uint8_t pin);
uint8_t digitalPinToInterrupt(uint8_t pin);
void tone(uint8_t pin, unsigned int freq, unsigned long duration = 0);
void noTone(uint8_t pin);
long random(long max);
long random(long min, long max);
long map(long x, long in_min, long in_max, long out_min, long out_max);
template <class T> T constrain(T x, T a, T b) { return x < a ? a : (x > b ? b : x); }

// ESP32 LEDC (PWM) API
uint32_t ledcSetup(uint8_t channel, uint32_t freq, uint8_t resolution_bits);
void ledcAttachPin(uint8_t pin, uint8_t channel);
bool ledcAttach(uint8_t pin, uint32_t freq, uint8_t resolution);
void ledcWrite(uint8_t channel_or_pin, uint32_t duty);

class String : public std::string {
 public:
  using std::string::string;
  String() = default;
  String(const std::string& s) : std::string(s) {}
  String(int v) : std::string(std::to_string(v)) {}
  String(long v) : std::string(std::to_string(v)) {}
  String(unsigned long v) : std::string(std::to_string(v)) {}
  String(float v, int = 2) : std::string(std::to_string(v)) {}
  String(double v, int = 2) : std::string(std::to_string(v)) {}
  int toInt() const;
  float toFloat() const;
  const char* c_str() const { return std::string::c_str(); }
};

class Print {
 public:
  template <class T> size_t print(const T&, int = 10) { return 0; }
  template <class T> size_t println(const T&, int = 10) { return 0; }
  size_t println() { return 0; }
  size_t printf(const char*, ...) { return 0; }
  size_t write(uint8_t) { return 0; }
};

class HardwareSerial : public Print {
 public:
  void begin(unsigned long baud, uint32_t config = 0, int8_t rx = -1, int8_t tx = -1);
  void end();
  int available();
  int read();
  int peek();
  void flush();
  String readStringUntil(char terminator);
  explicit operator bool() const { return true; }
};
extern HardwareSerial Serial;
extern HardwareSerial Serial1;
extern HardwareSerial Serial2;
