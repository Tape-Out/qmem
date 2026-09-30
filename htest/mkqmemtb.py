"""qmem 的行为测试台：引脚上挂一颗 Flash、一颗 PSRAM 的模型，按 SPI 模式 0 从引脚解码。

模型逐拍查电气规矩，违反就报：片子选中时 IO2、IO3（/WP、/HOLD）必须是高；两根片选不许同时落；
MOSI 只在 SCK 低时换；PSRAM 要先收 0x66、0x99 复位，第一条命令不早于上电 200·mhz 拍；
容量以外的地址与写 Flash 要回错，而且一根片选都不动。

序列：读 Flash 首尾两字 · 写 PSRAM 一字再读回 · 字节与半字选通只改那几个字节 · 连写四字再读回。
容量、分频与主频取这一点的旋钮；模型只存 1 KiB，高位地址折到低 10 位。两次片选之间的间隔按 50 ns 折成拍数查。
"""
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out.mkdir(parents=True, exist_ok=True)
cfg = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
label = cfg.get("label", "")
k = cfg.get("knobs") or {}
div = int(k.get("div", 2))
mhz = int(k.get("mhz", 50))
FB = int(k.get("flashBits", 24))
RB = int(k.get("ramBits", 23))

RAM = 1 << 24
BOOT = 200 * mhz
GAP = max(2, -(-50 * mhz // 1000))


def fpat(a: int) -> int:
    """Flash 第 a 字节的内容。"""
    return (a * 7 + 3) & 0xFF


def fword(a: int) -> int:
    return sum(fpat(a + i) << (8 * i) for i in range(4))


mem = {}
ops = []   # (写?, 地址, 数据, 选通, 期望的读数或 None, 期望回错)


def rd(a, want=None, err=False):
    ops.append((False, a, 0, 0xF, want, err))


def wr(a, d, s=0xF, err=False):
    ops.append((True, a, d, s, None, err))
    if not err:
        for i in range(4):
            if s >> i & 1:
                mem[(a & ~3) - RAM + i] = d >> (8 * i) & 0xFF


def ramword(a):
    return sum(mem.get(a - RAM + i, 0) << (8 * i) for i in range(4))


rd(0x0, fword(0))
rd((1 << FB) - 4, fword((1 << FB) - 4))
if FB < 24:
    rd(1 << FB, err=True)
wr(0x10, 0x1234, err=True)
wr(RAM + 0x10, 0x11223344)
rd(RAM + 0x10, ramword(RAM + 0x10))
wr(RAM + 0x10, 0x0000AA00, 0b0010)
rd(RAM + 0x10, ramword(RAM + 0x10))
wr(RAM + 0x10, 0xBEEF0000, 0b1100)
rd(RAM + 0x10, ramword(RAM + 0x10))
wr(RAM + 0x10, 0x000000CD, 0b0001)
rd(RAM + 0x10, ramword(RAM + 0x10))
if RB < 24:
    rd(RAM + (1 << RB), err=True)
TOP = RAM + (1 << RB) - 16
for j in range(4):
    wr(TOP + 4 * j, 0xA0B0C0D0 + j)
for j in range(4):
    rd(TOP + 4 * j, ramword(TOP + 4 * j))
rd(0x200, fword(0x200))
N = len(ops)
ERRS = sum(1 for o in ops if o[5])
SEL = 2 + N - ERRS   # 两次复位加不回错的每一笔

rows = "\n".join(
    f"    {i}: return Op {{ w: {'True' if w else 'False'}, a: 25'h{a:07X}, d: 32'h{d:08X}, "
    f"s: 4'h{s:X}, want: 32'h{(want or 0):08X}, chk: {'True' if want is not None else 'False'}, "
    f"err: {'True' if err else 'False'} }};"
    for i, (w, a, d, s, want, err) in enumerate(ops))

(out / f"Qmem{label}Tb.bsv").write_text(f'''package Qmem{label}Tb;

import RegFile::*;
import RegIf::*;
import Qmem::*;

// 由 htest/mkqmemtb.py 生成，勿手改。div={div} mhz={mhz} flashBits={FB} ramBits={RB}

typedef struct {{
  Bool w; Bit#(25) a; Bit#(32) d; Bit#(4) s; Bit#(32) want; Bool chk; Bool err;
}} Op deriving (Bits);

function Op op(Bit#(8) i);
  case (i)
{rows}
    default: return unpack(0);
  endcase
endfunction

function Bit#(8) fpat(Bit#(10) a) = truncate(a * 7 + 3);

(* synthesize *)
module mkQmem{label}Tb(Empty);
  QmemIfc#(25, 32, {FB}, {RB}, {div}, {mhz}) dut <- mkQmem(QmemCfg {{ }});
  RegFile#(Bit#(10), Bit#(8)) ram <- mkRegFileFull;

  Reg#(Bit#(32)) cyc  <- mkReg(0);
  Reg#(Bool)     bad  <- mkReg(False);
  Reg#(Bit#(8))  i    <- mkReg(0);
  Reg#(Bool)     wait_ <- mkReg(False);

  // 片子一侧的模型状态
  Reg#(Bit#(1))  sckp  <- mkReg(0);
  Reg#(Bit#(1))  mop   <- mkReg(0);
  Reg#(Bit#(2))  csp   <- mkReg(2'b11);
  Reg#(Bit#(8))  nb    <- mkReg(0);     // 本次片选里收了几位
  Reg#(Bit#(8))  sh    <- mkReg(0);
  Reg#(Bit#(8))  cmd   <- mkReg(0);
  Reg#(Bit#(24)) adr   <- mkReg(0);
  Reg#(Bit#(8))  obyte <- mkReg(0);
  Reg#(Bit#(3))  obit  <- mkReg(7);
  Reg#(Bit#(1))  miso  <- mkReg(0);
  Reg#(Bit#(2))  rstSeen <- mkReg(0);
  Reg#(Bit#(8))  hiRun <- mkReg(255);   // 片选放开之后高了几拍
  Reg#(Bit#(16)) selects <- mkReg(0);   // 片选落下的次数
  Reg#(Bit#(32)) upc   <- mkReg(0);     // 引脚接上之后的拍数，被测件从这一拍才开始走

  rule tick;
    cyc <= cyc + 1;
    if (cyc > {BOOT + 400000}) begin
      $display("TIMEOUT at op %0d", i);
      $finish(1);
    end
  endrule

  // 模型从第 0 拍就接在引脚上：被测件的上电等待与 PSRAM 复位都得看得见。只读写过的字节
  rule chip;
    let sck = dut.pins.sck;
    let cs = dut.pins.cs_n;
    let o = dut.pins.io_o;
    let oe = dut.pins.io_oe;
    Bit#(1) mo = o[0];
    Bool nbad = bad;
    Bool fl = cs[0] == 0;
    Bool ps = cs[1] == 0;
    if (fl && ps) begin $display("FAIL both chip selects low at cycle %0d", cyc); nbad = True; end
    if ((fl || ps) && (o[3:2] != 2'b11 || oe[3:2] != 2'b11)) begin $display("FAIL IO2 or IO3 not driven high while selected at cycle %0d", cyc); nbad = True; end
    if ((fl || ps) && (oe[0] != 1 || oe[1] != 0)) begin $display("FAIL IO0 must drive and IO1 must listen at cycle %0d", cyc); nbad = True; end
    if (sck == 1 && sckp == 1 && mo != mop && (fl || ps)) begin $display("FAIL MOSI changed while SCK was high at cycle %0d", cyc); nbad = True; end
    if (cs != 2'b11 && csp == 2'b11) begin
      selects <= selects + 1;
      if (hiRun < {GAP}) begin $display("FAIL chip select high for less than {GAP} cycles at cycle %0d", cyc); nbad = True; end
      if (upc < {BOOT}) begin $display("FAIL a command before the power-up wait at cycle %0d", cyc); nbad = True; end
    end
    upc <= upc + 1;
    hiRun <= (cs == 2'b11) ? ((hiRun == 255) ? 255 : hiRun + 1) : 0;

    Bit#(8) nnb = nb;
    Bit#(8) nsh = sh;
    Bit#(8) ncmd = cmd;
    Bit#(24) nadr = adr;
    Bit#(8) nob = obyte;
    Bit#(3) nobit = obit;
    Bit#(1) nmiso = miso;
    if (!fl && !ps) begin
      nnb = 0;
      nobit = 7;
    end else if (sck == 1 && sckp == 0) begin
      Bit#(8) b = {{sh[6:0], mo}};
      nsh = b;
      nnb = nb + 1;
      if (nb == 7) begin
        ncmd = b;
        if (ps && rstSeen == 0 && b != 8'h66) begin $display("FAIL PSRAM got a command before reset enable at cycle %0d", cyc); nbad = True; end
        if (ps && rstSeen == 1 && b != 8'h99) begin $display("FAIL PSRAM reset enable not followed by reset at cycle %0d", cyc); nbad = True; end
        if (ps && b == 8'h66 && rstSeen == 0) rstSeen <= 1;
        if (ps && b == 8'h99 && rstSeen == 1) rstSeen <= 2;
        if (fl && b != 8'h03) begin $display("FAIL the flash only takes 0x03 here at cycle %0d", cyc); nbad = True; end
      end else if (nb >= 8 && nb < 32) begin
        nadr = {{adr[22:0], mo}};
      end else if (nb >= 32 && cmd == 8'h02 && nb[2:0] == 7) begin
        ram.upd(truncate(adr), b);
        nadr = adr + 1;
      end
    end else if (sck == 0 && sckp == 1 && nb >= 32 && cmd == 8'h03) begin
      // 下降沿换位；每一字节的第一位在它前一个字节最后一位的下降沿后放出
      Bit#(8) cur = (nobit == 7) ? (fl ? fpat(truncate(adr)) : ram.sub(truncate(adr))) : obyte;
      nmiso = cur[nobit];
      nob = cur;
      if (nobit == 0) begin
        nadr = adr + 1;
        nobit = 7;
      end else nobit = obit - 1;
    end
    nb <= nnb; sh <= nsh; cmd <= ncmd; adr <= nadr; obyte <= nob; obit <= nobit; miso <= nmiso;
    sckp <= sck; mop <= mo; csp <= cs;
    bad <= nbad;
    dut.pins.io_in({{2'b11, nmiso, 1'b0}});
  endrule

  let cur = op(i);

  rule drive;
    dut.mem.req(i < {N}, RegReq {{ addr: cur.a, write: cur.w, wdata: cur.d, wstrb: cur.s }});
  endrule

  rule answer (i < {N} && dut.mem.rspValid);
    let r = dut.mem.rsp;
    if (r.err != cur.err) begin
      $display("FAIL op %0d at %h: err %0d, want %0d", i, cur.a, r.err, cur.err);
      bad <= True;
    end else if (cur.chk && r.rdata != cur.want) begin
      $display("FAIL op %0d at %h: read %h, want %h", i, cur.a, r.rdata, cur.want);
      bad <= True;
    end
    i <= i + 1;
  endrule

  rule done (i == {N});
    // 回错的那几笔不动片选
    if (selects != {SEL}) begin
      $display("FAIL %0d chip selects, want {SEL}", selects);
      $finish(1);
    end
    if (rstSeen != 2) begin
      $display("FAIL the PSRAM was never reset");
      $finish(1);
    end
    if (bad) $finish(1);
    $display("PASS qmem div={div}: {N} accesses, flash reads, PSRAM strobed writes, errors without a select");
    $finish(0);
  endrule
endmodule

endpackage
''', encoding="utf-8")
