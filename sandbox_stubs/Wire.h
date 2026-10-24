// Minimal Wire (I2C) stub for syntax checks.
#pragma once
#include "Arduino.h"

class TwoWire : public Print {
 public:
  bool begin(int sda = -1, int scl = -1, uint32_t frequency = 0);
  void setClock(uint32_t frequency);
  void beginTransmission(uint8_t address);
  uint8_t endTransmission(bool sendStop = true);
  uint8_t requestFrom(uint8_t address, uint8_t quantity, bool sendStop = true);
  size_t write(uint8_t data);
  size_t write(const uint8_t* data, size_t len);
  int available();
  int read();
};
extern TwoWire Wire;
