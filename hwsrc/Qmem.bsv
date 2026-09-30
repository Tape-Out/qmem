package Qmem;

// 片外 SPI Flash 与 SPI PSRAM 的控制器：程序在 Flash 里就地执行，数据在 PSRAM。
// 两颗共用 SCK 与 IO，各一根片选。地址窗的低一半是 Flash（只读，写回错），高一半是 PSRAM；
// 片子容量以外回错，不发命令。每一笔都要等片子，所以控制口只有会停顿的 RegTarget。
//
// 单线 SPI、模式 0：Flash 用 0x03 读，PSRAM 用 0x03 读、0x02 写（APS6404L、ESP-PSRAM64H、
// W25Q 系列都认）。总线上的字是小端的：从字对齐的地址起连读四字节，第一个字节是最低位。
// 写只发选通覆盖的那几个字节，选通要连续（核的 sb、sh、sw 都是）。
//
// IO2、IO3 在单线下是 Flash 的 /WP 与 /HOLD，一直驱高；HOLD 被拉低，Flash 会停在半截。
// SCK 是系统时钟的 1/(2·div)。引脚都从寄存器出去，ph 是当前引脚上 SCK 的相位：
// 前 div 拍低、后 div 拍高。MOSI 在下降沿换；MISO 在高电平的最后一拍采，片子在下降沿
// 之后才换位，这一拍离它换位最远。

import RegIf::*;

typedef struct {
  Bit#(0) none;
} QmemCfg;

interface QmemPins;
  (* always_ready, result = "sck" *)   method Bit#(1) sck;
  (* always_ready, result = "cs_n" *)  method Bit#(2) cs_n;    // [0] Flash、[1] PSRAM
  (* always_ready, result = "io_o" *)  method Bit#(4) io_o;
  (* always_ready, result = "io_oe" *) method Bit#(4) io_oe;
  (* always_ready, always_enabled, prefix = "" *)
  method Action io_in((* port = "io_i" *) Bit#(4) v);
endinterface

interface QmemIfc#(numeric type aw, numeric type dw,
                   numeric type flashBits, numeric type ramBits, numeric type div, numeric type mhz);
  interface RegTarget#(aw, dw) mem;
  interface QmemPins pins;
endinterface

typedef enum { Boot, Idle, Shift, Gap } St deriving (Bits, Eq, FShow);

module mkQmem#(QmemCfg cfg)(QmemIfc#(aw, dw, flashBits, ramBits, div, mhz))
    provisos (Add#(0, dw, 32), Add#(0, TDiv#(dw, 8), 4),
              Add#(_a, flashBits, TSub#(aw, 1)), Add#(_b, ramBits, TSub#(aw, 1)),
              Add#(_c, TSub#(aw, 1), 24), Add#(1, _d, aw));

  // PSRAM 上电 150 µs 后才收命令（APS6404L 表 5），Flash 要 20 µs（W25Q128JV tVSL），取大的再留余量
  Integer boot = 200 * valueOf(mhz);
  Integer half = valueOf(div);
  // 两次片选之间：PSRAM 要 18 ns（APS6404L tCPH），Flash 写类命令要 50 ns（W25Q128JV tSHSL），按 50 ns 算
  Integer gap = max(2, (50 * valueOf(mhz) + 999) / 1000);

  Reg#(St)        st    <- mkReg(Boot);
  Reg#(UInt#(16)) t     <- mkReg(fromInteger(boot));
  Reg#(Bit#(2))   rst   <- mkReg(2);      // PSRAM 的复位：先 0x66 再 0x99
  Reg#(Bit#(2))   csR   <- mkReg(2'b11);
  Reg#(Bit#(1))   sckR  <- mkReg(0);
  Reg#(Bit#(1))   moR   <- mkReg(0);
  Reg#(Bit#(64))  tx    <- mkReg(0);
  Reg#(UInt#(7))  nout  <- mkReg(0);
  Reg#(UInt#(7))  nin   <- mkReg(0);
  Reg#(Bit#(32))  rxR   <- mkReg(0);
  Reg#(UInt#(8))  ph    <- mkReg(0);
  Reg#(Bool)      reply <- mkReg(False);  // 这一笔完了要答（复位命令不答）
  Reg#(Bool)        ans  <- mkReg(False);
  Reg#(RegRsp#(dw)) rspR <- mkReg(RegRsp { rdata: 0, err: False });

  Wire#(Bit#(4))         ioIn  <- mkBypassWire;
  Wire#(Bool)            takeV <- mkDWire(False);
  Wire#(RegReq#(aw, dw)) takeR <- mkDWire(unpack(0));

  Bool canTake = st == Idle && !ans;

  // 选通覆盖的第一个与最后一个字节
  function Tuple2#(UInt#(2), UInt#(2)) span(Bit#(4) s);
    UInt#(2) f = 0;
    UInt#(2) l = 0;
    Bool got = False;
    for (Integer i = 0; i < 4; i = i + 1)
      if (s[i] == 1) begin
        if (!got) f = fromInteger(i);
        l = fromInteger(i);
        got = True;
      end
    return tuple2(f, l);
  endfunction

  rule step;
    St ns = st;
    UInt#(16) nt = t;
    Bit#(2) nrst = rst;
    Bit#(2) ncs = csR;
    Bit#(1) nsck = sckR;
    Bit#(1) nmo = moR;
    Bit#(64) ntx = tx;
    UInt#(7) no = nout;
    UInt#(7) ni = nin;
    Bit#(32) nrx = rxR;
    UInt#(8) nph = ph;
    Bool nrep = reply;
    Bool nans = False;
    RegRsp#(dw) nrsp = rspR;
    Bool go = False;          // 这一拍起一笔：片选落下、第一位放上 MOSI

    case (st)
      Boot: begin
        if (t != 0) nt = t - 1;
        else if (rst != 0) begin
          ncs = 2'b01;
          ntx = {(rst == 2) ? 8'h66 : 8'h99, 56'h0};
          no = 8; ni = 0; nrep = False;
          nrst = rst - 1;
          go = True;
        end else ns = Idle;
      end
      Idle: if (takeV) begin
        let q = takeR;
        Bool ram = q.addr[valueOf(aw) - 1] == 1;
        Bit#(TSub#(aw, 1)) off = truncate(q.addr);
        // 容量在 aw 位里比：16 MiB 的片子，2^24 放不进窗口一半的那 24 位
        Bit#(aw) cap = ram ? fromInteger(2 ** valueOf(ramBits))
                           : fromInteger(2 ** valueOf(flashBits));
        Bit#(24) a = zeroExtend(off);
        match {.f, .l} = span(q.wstrb);
        if (zeroExtend(off) >= cap || (q.write && !ram) || (q.write && q.wstrb == 0)) begin
          nans = True;
          nrsp = RegRsp { rdata: 0, err: True };
        end else if (q.write) begin
          // 从第一个选通字节的地址起写，字节按地址递增上线：移掉前面没选通的字节，再倒成先低后高
          Bit#(5) by = zeroExtend(pack(f)) << 3;
          Bit#(32) w = q.wdata >> by;
          UInt#(7) n = unpack(zeroExtend(pack(l - f))) + 1;
          ntx = {8'h02, a[23:2], pack(f), w[7:0], w[15:8], w[23:16], w[31:24]};
          no = 32 + 8 * n; ni = 0;
          ncs = 2'b01;
          nrep = True; go = True;
        end else begin
          ntx = {8'h03, a[23:2], 2'b00, 32'h0};
          no = 32; ni = 32;
          ncs = ram ? 2'b01 : 2'b10;
          nrep = True; go = True;
        end
      end
      Shift: begin
        if (ph == fromInteger(half - 1)) begin
          nsck = 1;
          nph = ph + 1;
        end else if (ph == fromInteger(2 * half - 1)) begin
          // 高电平的最后一拍：出的那一位片子已在上升沿采走，收的那一位这时采
          if (no != 0) begin
            ntx = tx << 1;
            no = no - 1;
          end else begin
            nrx = {rxR[30:0], ioIn[1]};
            ni = ni - 1;
          end
          nsck = 0;
          nph = 0;
          nmo = (no != 0) ? ntx[63] : 0;
          if (no == 0 && ni == 0) ns = Gap;
        end else nph = ph + 1;
      end
      Gap: begin
        // SCK 已落回低；片选再保持一拍才放开，放开后至少空 gap 拍才落下一次
        if (ph == 0) nph = 1;
        else if (ph == 1) begin
          ncs = 2'b11;
          nph = 2;
          if (reply) begin
            nans = True;
            // 收进来的四个字节先到的是最低位
            nrsp = RegRsp { rdata: {rxR[7:0], rxR[15:8], rxR[23:16], rxR[31:24]}, err: False };
          end
        end else if (ph < fromInteger(gap)) nph = ph + 1;
        else ns = (rst != 0) ? Boot : Idle;
      end
    endcase

    if (go) begin
      nsck = 0;
      nmo = ntx[63];
      nph = 0;
      ns = Shift;
    end

    st <= ns; t <= nt; rst <= nrst; csR <= ncs; sckR <= nsck; moR <= nmo; tx <= ntx;
    nout <= no; nin <= ni; rxR <= nrx; ph <= nph; reply <= nrep; ans <= nans; rspR <= nrsp;
  endrule

  interface RegTarget mem;
    method Action req(Bool valid, RegReq#(aw, dw) q);
      if (valid && canTake) begin
        takeV <= True;
        takeR <= q;
      end
    endmethod
    method Bool ready = canTake;
    method Bool rspValid = ans;
    method RegRsp#(dw) rsp = rspR;
  endinterface

  interface QmemPins pins;
    method Bit#(1) sck = sckR;
    method Bit#(2) cs_n = csR;
    method Bit#(4) io_o = {2'b11, 1'b0, moR};
    method Bit#(4) io_oe = 4'b1101;
    method Action io_in(Bit#(4) v);
      ioIn <= v;
    endmethod
  endinterface
endmodule

endpackage
