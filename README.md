# qmem

Execute in place from an SPI flash and keep data in an SPI PSRAM. The two chips share SCK and the four IO pins and have a chip select each; on the bus they are one window.

![maturity](https://img.shields.io/badge/maturity-simulated-yellow) ![license](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0%20OR%20MulanPSL--2.0-blue)

Part of the [Tape-Out](https://github.com/Tape-Out) IP library: Bluespec IP over the bus-neutral contracts in [`hwcore`](https://github.com/Tape-Out/hwcore), assembled by [`xirang`](https://github.com/Tape-Out/xirang). It needs no memory macro, so a core can run from it on any process.

## Window

| Offset | What | Access |
|:--:|:--:|:--:|
| `0` to `2^flashBits - 1` | flash | read; a write answers an error |
| `2^24` to `2^24 + 2^ramBits - 1` | PSRAM | read and write, byte strobes |
| anything else | nothing | an error, and no chip select moves |

Words are little-endian: a read fetches four bytes from the word-aligned address, the first byte in the low bits. A write sends only the strobed bytes, which must be contiguous (as `sb`, `sh` and `sw` are).

## On the board

| Pin | Connects to |
|:--:|:--:|
| `sck` | SCK of both chips |
| `cs_n[0]` | /CS of the flash (W25Q128JV or any flash that takes `0x03`) |
| `cs_n[1]` | /CE of the PSRAM (APS6404L, ESP-PSRAM64H) |
| `io[0]` | DI of the flash, SI of the PSRAM |
| `io[1]` | DO of the flash, SO of the PSRAM |
| `io[2]`, `io[3]` | /WP and /HOLD of the flash, SIO2 and SIO3 of the PSRAM; driven high |

The pins are single-line SPI mode 0. After reset the controller waits 200 µs, as the PSRAM needs 150 µs and the flash 20 µs, then sends the PSRAM reset (`0x66`, `0x99`).

## Knobs

| Knob | Default | Meaning |
|:--:|:--:|:--:|
| `flashBits` | 24 | flash size, log2 of bytes (W25Q128 is 24) |
| `ramBits` | 23 | PSRAM size (APS6404L is 23) |
| `div` | 2 | SCK is the clock divided by `2·div`; `0x03` reads are good to 50 MHz on the flash and 33 MHz on the PSRAM |
| `mhz` | 50 | the clock, for the power-up wait and the chip-select gap (50 ns) |

## Testing

`htest/mkqmemtb.py` puts a flash and a PSRAM model on the pins that decode SPI mode 0 and check the electrical rules on every cycle:

- IO2 and IO3 are high while a chip is selected.
- The two selects are never low together.
- MOSI changes only while SCK is low.
- The PSRAM is reset before anything else.
- The first command comes after the power-up wait.
- Two selects are at least 50 ns apart.

The sequence reads the ends of the flash, writes and reads the PSRAM with word, half-word and byte strobes, and checks the out-of-range and flash-write errors. Twelve single-line mutations of the design each fail it.

## License

任选其一：

- [MIT](LICENSE-MIT)
- [Apache 2.0](LICENSE-APACHE)
- [木兰宽松许可证 第2版](LICENSE-MULAN)

`SPDX-License-Identifier: MIT OR Apache-2.0 OR MulanPSL-2.0`

除非另行说明，你提交的贡献按上述三者同时授权，不附加其他条件。
