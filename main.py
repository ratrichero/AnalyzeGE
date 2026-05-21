import logging
import asyncio
import re
import ccxt.async_support as ccxt
import pandas as pd
from cachetools import TTLCache
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
)

# --- CẤU HÌNH CƠ BẢN ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Thay Token của bồ tèo vào đây
TELEGRAM_BOT_TOKEN = "8895435477:AAEMGY0vpdNzreyMF7LGIvi1aXIo-KO9Sho"

# ==========================================
# 1. KHỐI LẤY DỮ LIỆU SÀN BINANCE (DÙNG CCXT)
# ==========================================
class BinanceDataFactory:
    def __init__(self):
        # Dùng CCXT né lỗi IP Restricted cực tốt, không tự động ping khi khởi tạo
        self.exchange = ccxt.binance({
            'options': {'defaultType': 'future'},
            'enableRateLimit': True,
        })
        self.candle_cache = TTLCache(maxsize=1000, ttl=60) # Cache 60 giây chống block API

    async def get_candles(self, symbol, interval, limit=150):
        # Format lại symbol cho chuẩn CCXT (VD: BTCUSDT -> BTC/USDT)
        ccxt_symbol = symbol.replace('USDT', '') + '/USDT'
        cache_key = f"{symbol}_{interval}"
        
        if cache_key in self.candle_cache:
            return self.candle_cache[cache_key]
        
        try:
            ohlcv = await self.exchange.fetch_ohlcv(ccxt_symbol, timeframe=interval, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            self.candle_cache[cache_key] = df
            return df
        except Exception as e:
            logger.error(f"Lỗi tải dữ liệu CCXT cho {symbol}: {e}")
            return None

    def format_price(self, symbol, price):
        # Đơn giản hóa việc format (hiển thị 5 số thập phân nếu giá quá nhỏ, 2 nếu giá lớn)
        if price < 0.1:
            return f"{price:.6f}"
        elif price < 10:
            return f"{price:.4f}"
        else:
            return f"{price:.2f}"

# ==========================================
# 2. KHỐI PHÂN TÍCH KỸ THUẬT (PANDAS THUẦN)
# ==========================================
class MarketAnalyzer:
    def __init__(self, factory):
        self.factory = factory

    async def analyze_single_frame(self, symbol, interval):
        df = await self.factory.get_candles(symbol, interval)
        if df is None or df.empty or len(df) < 50:
            return None

        last_close = df['close'].iloc[-1]

        # --- Tự tính toán Indicators bằng Pandas ---
        # 1. EMA 20 & 50
        df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
        ema20 = df['EMA_20'].iloc[-1]
        ema50 = df['EMA_50'].iloc[-1]

        # 2. RSI 14 (Chuẩn Wilder's)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        rsi = df['RSI'].iloc[-1]

        # 3. Bollinger Bands (20, 2)
        df['BB_mid'] = df['close'].rolling(window=20).mean()
        df['BB_std'] = df['close'].rolling(window=20).std()
        bb_upper = df['BB_mid'].iloc[-1] + (2 * df['BB_std'].iloc[-1])
        bb_lower = df['BB_mid'].iloc[-1] - (2 * df['BB_std'].iloc[-1])

        # --- Phân loại Trạng Thái ---
        # Nhóm 1: Xu hướng
        if last_close > ema20 > ema50:
            trend = "🟢 TĂNG MẠNH (Bullish)"
            trend_score = 2
        elif last_close > ema50:
            trend = "🟢 TĂNG NHẸ"
            trend_score = 1
        elif last_close < ema20 < ema50:
            trend = "🔴 GIẢM MẠNH (Bearish)"
            trend_score = -2
        else:
            trend = "🟡 GIẢM NHẸ / ĐI NGANG"
            trend_score = -1

        # Nhóm 2: Động lượng
        if rsi > 70: momentum = f"🔥 QUÁ MUA ({rsi:.1f})"
        elif rsi < 30: momentum = f"❄️ QUÁ BÁN ({rsi:.1f})"
        else: momentum = f"⚖️ TRUNG TÍNH ({rsi:.1f})"

        # Nhóm 3: Biến động
        bb_bandwidth = ((bb_upper - bb_lower) / df['BB_mid'].iloc[-1]) * 100
        volatility = "💥 CAO (Mở băng)" if bb_bandwidth > 5 else "💤 THẤP (Bóp băng - Sideway)"

        # Nhóm 4: Khối lượng
        avg_vol = df['volume'].iloc[-20:-1].mean()
        last_vol = df['volume'].iloc[-1]
        volume_status = "🐋 ĐỘT BIẾN (Gấp đôi TB)" if last_vol > avg_vol * 2 else "💎 ỔN ĐỊNH"

        # Nhóm 5: Hỗ trợ / Kháng cự cứng (30 nến)
        support = df['low'].rolling(window=30).min().iloc[-1]
        resistance = df['high'].rolling(window=30).max().iloc[-1]

        # --- Tính toán Risk:Reward Động ---
        total_range = resistance - support
        
        # Thiết lập hệ số quản lý vốn (SL % và TP % tối thiểu dựa trên Entry)
        if interval == '15m':
            sl_pct, tp1_pct, tp2_pct = 0.003, 0.006, 0.012  # SL 0.3%, TP1 0.6%, TP2 1.2% (R:R chuẩn 1:2)
        elif interval == '1h':
            sl_pct, tp1_pct, tp2_pct = 0.006, 0.012, 0.025  # SL 0.6%, TP1 1.2%, TP2 2.5%
        elif interval == '4h':
            sl_pct, tp1_pct, tp2_pct = 0.015, 0.030, 0.060  # SL 1.5%, TP1 3.0%, TP2 6.0%
        else: # Khung 1D
            sl_pct, tp1_pct, tp2_pct = 0.030, 0.060, 0.120  # SL 3.0%, TP1 6.0%, TP2 12.0%

        price_step = last_close * 0.002
        
        # --- XỬ LÝ CHIẾN LƯỢC CHO XU HƯỚNG TĂNG ---
        if trend_score > 0:
            entry_normal = last_close - price_step
            tp1_normal = resistance
            final_sl = support * 0.995
            
            risk_normal = entry_normal - final_sl
            reward_normal = tp1_normal - entry_normal
            
            # KIỂM TRA R:R: Nếu giá quá sát cản (Ăn ít lỗ nhiều), chuyển sang LONG BREAKOUT
            if reward_normal < risk_normal:
                entry_breakout = resistance * 1.002
                # Tính toán mục tiêu TUYỆT ĐỐI dựa trên giá trị Entry mới
                sl_breakout = entry_breakout * (1 - sl_pct)
                tp1_breakout = entry_breakout * (1 + tp1_pct)
                tp2_breakout = entry_breakout * (1 + tp2_pct)
                
                trade_plan = (
                    f"🔥 **Chiến lược: LONG BREAKOUT (MUA PHÁ VỠ - KHUNG {interval.upper()})**\n"
                    "⚠️ *Trạng thái:* Giá quá sát Kháng cự, không mua đuổi. Chờ phá vỡ hẳn để kích hoạt!\n"
                    f" ├ 🟢 Điểm vào (Entry): `{self.factory.format_price(symbol, entry_breakout)}` (Khi nến đóng trên cản)\n"
                    f" ├ 🔴 Cắt lỗ (Stop Loss): `{self.factory.format_price(symbol, sl_breakout)}` (R:R chuẩn khung nhỏ)\n"
                    f" └ 🎯 Chốt lời (Take Profit): TP1: `{self.factory.format_price(symbol, tp1_breakout)}` | TP2: `{self.factory.format_price(symbol, tp2_breakout)}`"
                )
            else:
                trade_plan = (
                    f"🔥 **Chiến lược: LONG (MUA THUẬN XU HƯỚNG - KHUNG {interval.upper()})**\n"
                    f" ├ 🟢 Điểm vào (Entry hồi): `{self.factory.format_price(symbol, entry_normal)}`\n"
                    f" ├ 🔴 Cắt lỗ (Stop Loss): `{self.factory.format_price(symbol, final_sl)}` (Dưới hỗ trợ cứng)\n"
                    f" └ 🎯 Chốt lời (Take Profit): TP1: `{self.factory.format_price(symbol, tp1_normal)}` | TP2: `{self.factory.format_price(symbol, entry_normal + (risk_normal * 2))}`"
                )
            
        # --- XỬ LÝ CHIẾN LƯỢC CHO XU HƯỚNG GIẢM ---
        elif trend_score < 0:
            entry_normal = last_close + price_step
            tp1_normal = support
            final_sl = resistance * 1.005
            
            risk_normal = final_sl - entry_normal
            reward_normal = entry_normal - tp1_normal
            
            # KIỂM TRA R:R SHORT: Nếu giá quá sát hỗ trợ, chuyển sang SHORT BREAKDOWN
            if reward_normal < risk_normal:
                entry_breakdown = support * 0.998
                # Tính toán mục tiêu TUYỆT ĐỐI dựa trên giá trị Entry mới
                sl_breakdown = entry_breakdown * (1 + sl_pct)
                tp1_breakdown = entry_breakdown * (1 - tp1_pct)
                tp2_breakdown = entry_breakdown * (1 - tp2_pct)
                
                trade_plan = (
                    f"🔥 **Chiến lược: SHORT BREAKDOWN (BÁN PHÁ VỠ - KHUNG {interval.upper()})**\n"
                    "⚠️ *Trạng thái:* Giá quá sát Hỗ trợ, không bán đuổi. Chờ sập đáy để kích hoạt!\n"
                    f" ├ 🔴 Điểm vào (Entry): `{self.factory.format_price(symbol, entry_breakdown)}` (Khi nến đóng dưới hỗ trợ)\n"
                    f" ├ 🟢 Cắt lỗ (Stop Loss): `{self.factory.format_price(symbol, sl_breakdown)}` (R:R chuẩn khung nhỏ)\n"
                    f" └ 🎯 Chốt lời (Take Profit): TP1: `{self.factory.format_price(symbol, tp1_breakdown)}` | TP2: `{self.factory.format_price(symbol, tp2_breakdown)}`"
                )
            else:
                trade_plan = (
                    f"🔥 **Chiến lược: SHORT (BÁN THUẬN XU HƯỚNG - KHUNG {interval.upper()})**\n"
                    f" ├ 🔴 Điểm vào (Entry hồi): `{self.factory.format_price(symbol, entry_normal)}`\n"
                    f" ├ 🟢 Cắt lỗ (Stop Loss): `{self.factory.format_price(symbol, final_sl)}` (Trên kháng cự cứng)\n"
                    f" └ 🎯 Chốt lời (Take Profit): TP1: `{self.factory.format_price(symbol, tp1_normal)}` | TP2: `{self.factory.format_price(symbol, entry_normal - (risk_normal * 2))}`"
                )
                
        # --- XỬ LÝ CHIẾN LƯỢC KHI THỊ TRƯỜNG ĐI NGANG (SIDEWAY) ---
        else:
            entry_long = support * 1.002
            entry_short = resistance * 0.998
            
            # Đánh biên độ ngắn theo khung thời gian phát triển
            trade_plan = (
                f"⚠️ **Chiến lược: SWING TRADING (ĐÁNH TRONG BIÊN ĐỘ SIDEWAY - KHUNG {interval.upper()})**\n"
                f"🟩 **Kịch bản LONG:** Entry vùng hỗ trợ `{self.factory.format_price(symbol, entry_long)}` | SL `{self.factory.format_price(symbol, entry_long * (1 - sl_pct))}` | TP `{self.factory.format_price(symbol, resistance * 0.995)}`\n"
                f"🟥 **Kịch bản SHORT:** Entry vùng kháng cự `{self.factory.format_price(symbol, entry_short)}` | SL `{self.factory.format_price(symbol, entry_short * (1 + sl_pct))}` | TP `{self.factory.format_price(symbol, support * 1.005)}`"
            )

        return {
            "trend": trend, "rsi": rsi, "momentum": momentum,
            "volatility": volatility, "volume": volume_status,
            "support": support, "resistance": resistance,
            "close": last_close, "trade_plan": trade_plan
        }

# ==========================================
# 3. GIAO DIỆN & XỬ LÝ TELEGRAM
# ==========================================
binance_factory = BinanceDataFactory()
analyzer = MarketAnalyzer(binance_factory)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        welcome_msg = (
            "🤖 **GE ANALYZE FUTURE BOT SẴN SÀNG!**\n\n"
            "Nhập tên coin bạn muốn phân tích (Ví dụ: `BTC`, `SOL`, `PEPE`)"
        )
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"⚠️ Lỗi nghiêm trọng trong hàm start: {e}", exc_info=True)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return

        text = update.message.text.strip().upper()
        
        # Lọc ký tự thừa và thêm USDT nếu chưa có
        symbol_raw = re.sub(r'[^A-Z0-9]', '', text)
        if not symbol_raw:
            await update.message.reply_text("❌ Tên coin không hợp lệ. Vui lòng nhập lại!")
            return

        if not symbol_raw.endswith("USDT"):
            symbol = f"{symbol_raw}USDT"
        else:
            symbol = symbol_raw

        keyboard = [
            [
                InlineKeyboardButton("15m", callback_data=f"{symbol}_15m"),
                InlineKeyboardButton("1H", callback_data=f"{symbol}_1h"),
                InlineKeyboardButton("4H", callback_data=f"{symbol}_4h"),
                InlineKeyboardButton("1D", callback_data=f"{symbol}_1d"),
                InlineKeyboardButton("ALL", callback_data=f"{symbol}_MULTI")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🔍 Chọn khung thời gian phân tích cho **{symbol}**:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"⚠️ Lỗi xử lý tin nhắn (handle_message): {e}", exc_info=True)
        try:
            await update.message.reply_text("😥 Có lỗi hệ thống xảy ra khi phân tích coin này. Thử lại mã khác xem sao nhé!")
        except Exception:
            pass

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # 1. Bắt lỗi hết hạn session nút bấm sớm
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Lỗi Callback (Nút đã hết hạn hoặc bấm kép): {e}")
        return

    # 2. Toàn bộ logic phân tích được bọc an toàn
    try:
        data = query.data

        # Nhánh xử lý khi bấm nút quay lại
        if data == "BACK_TO_MENU":
            back_msg = (
                "🔄 **Mời bồ tèo nhập đồng coin mới!**\n"
                "Hãy gõ tên coin muốn phân tích tiếp theo (Ví dụ: `BTC`, `ETH`...)"
            )
            await query.edit_message_text(back_msg, parse_mode='Markdown')
            return

        # Cắt chuỗi lấy cặp giao dịch và khung thời gian
        symbol, interval = data.rsplit('_', 1)

        # --- ĐOẠN XỬ LÝ PHÂN TÍCH ĐA KHUNG (MULTI) ---
        if interval == "MULTI":
            await query.edit_message_text(f"⏳ Đang quét và đối chiếu dữ liệu 4 khung thời gian cho {symbol}...")
            
            # Lấy dữ liệu nhanh của cả 4 khung
            res15m = await analyzer.analyze_single_frame(symbol, "15m")
            res1h = await analyzer.analyze_single_frame(symbol, "1h")
            res4h = await analyzer.analyze_single_frame(symbol, "4h")
            res1d = await analyzer.analyze_single_frame(symbol, "1d")
            
            if not all([res15m, res1h, res4h, res1d]):
                await query.edit_message_text(f"❌ Không thể tải đủ dữ liệu đa khung cho {symbol}. Thử lại sau!")
                return
                
            # Đánh giá độ đồng thuận xu hướng
            trends = [res15m['trend'], res1h['trend'], res4h['trend'], res1d['trend']]
            tang_count = sum(1 for t in trends if "TĂNG" in t)
            giam_count = sum(1 for t in trends if "GIẢM" in t)
            
            if tang_count >= 3:
                consensus = "🟢 ĐỒNG THUẬN TĂNG MẠNH (Ưu tiên LONG khi hồi giá)"
            elif giam_count >= 3:
                consensus = "🔴 ĐỒNG THUẬN GIẢM MẠNH (Ưu tiên SHORT khi hồi giá)"
            else:
                consensus = "🟡 THỊ TRƯỜNG PHÂN HÓA / XUNG ĐỘT XU HƯỚNG (Nên đứng ngoài)"

            multi_report = (
                f"🌐 **BÁO CÁO PHÂN TÍCH ĐA KHUNG (MTF): {symbol}**\n"
                f"💵 Giá hiện tại: `{binance_factory.format_price(symbol, res1h['close'])}`\n"
                "-----------------------------------------\n"
                f"⏱️ **Khung ngắn (15m):** {res15m['trend']} | RSI: `{res15m['rsi']:.1f}`\n"
                f"⏱️ **Khung vừa (1H):** {res1h['trend']} | RSI: `{res1h['rsi']:.1f}`\n"
                f"⏱️ **Khung lớn (4H):** {res4h['trend']} | RSI: `{res4h['rsi']:.1f}`\n"
                f"⏱️ **Khung Ngày (1D):** {res1d['trend']} | RSI: `{res1d['rsi']:.1f}`\n"
                "-----------------------------------------\n"
                f"🎯 **KẾT LUẬN ĐA KHUNG:**\n"
                f"👉 **{consensus}**\n\n"
                "💡 *Lời khuyên:* Chỉ nên vào lệnh lớn khi khung ngắn (15m/1H) chạy cùng hướng với khung lớn (4H/1D)."
            )
            
            # Giao diện nút bấm quay lại
            keyboard = [
            [
                InlineKeyboardButton("15m", callback_data=f"{symbol}_15m"),
                InlineKeyboardButton("1H", callback_data=f"{symbol}_1h"),
                InlineKeyboardButton("4H", callback_data=f"{symbol}_4h"),
                InlineKeyboardButton("1D", callback_data=f"{symbol}_1d")
            ],
            [
                InlineKeyboardButton("⬅️ Menu chính", callback_data="BACK_TO_MENU")
            ]
        ]
            await query.edit_message_text(multi_report, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            return
        
        await query.edit_message_text(f"⏳ Đang phân tích dữ liệu {symbol} khung {interval}...")

        # Gọi hàm phân tích từ sàn
        result = await analyzer.analyze_single_frame(symbol, interval)
        
        if not result:
            await query.edit_message_text(f"❌ Không lấy được dữ liệu cho cặp {symbol}. Sàn Binance có thể không hỗ trợ mã này hoặc đang nghẽn mạng!")
            return

        # Tạo chuỗi nội dung báo cáo kết quả
        report = (
            f"📊 **PHÂN TÍCH ĐƠN KHUNG: {symbol} {interval}**\n"
            f"💵 Giá hiện tại: `{binance_factory.format_price(symbol, result['close'])}`\n"
            "-----------------------------------------\n"
            f"├── 🗺️ Xu hướng: {result['trend']}\n"
            f"├── ⚡ Động lượng: {result['momentum']}\n"
            f"├── 🌊 Biến động: {result['volatility']}\n"
            f"├── 📊 Khối lượng: {result['volume']}\n"
            "└── 🛡️ Vùng giá trị (S/R Cứng):\n"
            f"     ├ Kháng cự: `{binance_factory.format_price(symbol, result['resistance'])}`\n"
            f"     └ Hỗ trợ: `{binance_factory.format_price(symbol, result['support'])}`\n\n"
            f"{result['trade_plan']}"
        )

        # Tạo lại menu điều hướng
        keyboard = [
            [
                InlineKeyboardButton("15m", callback_data=f"{symbol}_15m"),
                InlineKeyboardButton("1H", callback_data=f"{symbol}_1h"),
                InlineKeyboardButton("4H", callback_data=f"{symbol}_4h"),
                InlineKeyboardButton("1D", callback_data=f"{symbol}_1d"),
                InlineKeyboardButton("ALL", callback_data=f"{symbol}_MULTI")
            ],
            [
                InlineKeyboardButton("⬅️ Menu chính", callback_data="BACK_TO_MENU")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(report, reply_markup=reply_markup, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"⚠️ Lỗi nghiêm trọng tại handle_callback: {e}", exc_info=True)
        try:
            # Nếu có lỗi (Ví dụ CCXT lỗi, tính toán lỗi mảng trống), bot báo về Telegram thay vì đứng im
            keyboard = [[InlineKeyboardButton("⬅️ Quay lại Menu", callback_data="BACK_TO_MENU")]]
            await query.edit_message_text(
                "💥 **Hệ thống phân tích gặp sự cố bất ngờ!**\n"
                "Có thể do dữ liệu nến trên sàn bị ngắt quãng. Bồ tèo hãy thử lại sau hoặc chọn coin khác nhé.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        except Exception:
            pass

# ==========================================
# 4. CHẠY BOT
# ==========================================
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    logger.info("🚀 BOT ĐANG CHẠY BẰNG CCXT (NÉ LỖI IP) VÀ PANDAS THUẦN...")
    app.run_polling()

if __name__ == '__main__':
    # Bản sửa lỗi khởi chạy loop chuẩn Python 3.10+
    try:
        main()
    except RuntimeError as e:
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            # Nếu loop đã chạy sẵn (một số môi trường đặc biệt), chạy thẳng hàm main qua loop cũ
            loop = asyncio.get_event_loop()
            loop.create_task(main())
        else:
            raise e