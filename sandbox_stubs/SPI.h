// Minimal SPI stub for syntax checks.
#pragma once
#include "Arduino.h"

#define MSBFIRST 1
#define LSBFIRST 0
#define SPI_MODE0 0x00

class SPISettings {
 public:
  SPISettings(uint32_t clock = 1000000, uint8_t bitOrder = MSBFIRST, uint8_t dataMode = SPI_MODE0) {}
};

class SPIClass {
 public:
  void begin(int8_t sck = -1, int8_t miso = -1, int8_t mosi = -1, int8_t ss = -1);
  void beginTransaction(SPISettings settings);
  void endTransaction();
  uint8_t transfer(uint8_t data);
};
extern SPIClass SPI;
