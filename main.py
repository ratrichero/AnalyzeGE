import os
import math
import logging
import asyncio
from decimal import Decimal, ROUND_DOWN
import pandas as pd
from cachetools import TTLCache

# Các thư viện Binance và Telegram
from binance.client import Client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters
)

# Cấu hình LOGGING
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# CONFIG ĐỊNH CẤU HÌNH HỆ THỐNG
USE_API_KEY = False  
BINANCE_API_KEY = ""
BINANCE_API_SECRET = ""
TELEGRAM_BOT_TOKEN = "8895435477:AAEMGY0vpdNzreyMF7LGIvi1aXIo-KO9Sho"

# -------------------------------------------------------------------------
# 1. DATA LAYER: QUẢN LÝ DỮ LIỆU BINANCE & TTL CACHE
# -------------------------------------------------------------------------
class BinanceDataFactory:
    def __init__(self):
        if USE_API_KEY and BINANCE_API_KEY and BINANCE_API_SECRET:
            self.client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
            logger.info("Khởi tạo Binance Client với API Key cá nhân.")
        else:
            self.client = Client("", "")
            logger.info("Khởi tạo Binance Client ở chế độ Public (Mặc định).")
            
        self.candle_cache = TTLCache(maxsize=1000, ttl=60)
        self.symbol_info = {}
        self.load_exchange_info()

    def load_exchange_info(self):
        try:
            info = self.client.futures_exchange_info()
            for s in info['symbols']:
                if s['contractType'] == 'PERPETUAL':
                    tick_size = next(f['tickSize'] for f in s['filters'] if f['filterType'] == 'PRICE_FILTER')
                    precision = abs(Decimal(str(tick_size)).normalize().as_tuple().exponent)
                    self.symbol_info[s['symbol'].upper()] = {
                        'pricePrecision': int(s['pricePrecision']),
                        'quantityPrecision': int(s['quantityPrecision']),
                        'tickSize': tick_size,
                        'decimal_places': precision
                    }
            logger.info("Đã tải thông tin cấu trúc cặp giao dịch Binance Futures thành công.")
        except Exception as e:
            logger.error(f"Lỗi khi tải exchange info: {e}")

    def format_price(self, symbol, price):
        info = self.symbol_info.get(symbol.upper())
        if not info:
            return f"{price:.4f}"
        places = info['decimal_places']
        return f"{price:.{places}f}"

    def get_candles(self, symbol, interval, limit=150):
        symbol = symbol.upper()
        cache_key = f"{symbol}_{interval}"
        
        if cache_key in self.candle_cache:
            return self.candle_cache[cache_key]
        
        try:
            bars = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            df = pd.DataFrame(bars, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume', 
                'close_time', 'quote_asset_volume', 'number_of_trades', 
                'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
            ])
            df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
            self.candle_cache[cache_key] = df
            return df
        except Exception as e:
            logger.error(f"Lỗi API Binance khi lấy nến {symbol} [{interval}]: {e}")
            return None

# -------------------------------------------------------------------------
# 2. ANALYSIS ENGINE: PHÂN TÍCH TIÊU CHUẨN KỸ THUẬT
# -------------------------------------------------------------------------
class MarketAnalyzer:
    def __init__(self, data_factory: BinanceDataFactory):
        self.factory = data_factory

    def analyze_single_frame(self, symbol, interval):
        df = self.factory.get_candles(symbol, interval)
        if df is None or df.empty or len(df) < 50:
            return None

        last_close = df['close'].iloc[-1]

        # --- TỰ TÍNH EMA VÀ RSI BẰNG PANDAS THUẦN (KHÔNG DÙNG PANDAS_TA) ---
        # Tính EMA 20 và EMA 50
        df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
        ema20 = df['EMA_20'].iloc[-1]
        ema50 = df['EMA_50'].iloc[-1]
        
        # Tính RSI 14 chuẩn Wilder's
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        rsi = df['RSI'].iloc[-1]

        # Tính Bollinger Bands (Độ lệch chuẩn std=2)
        df['BB_mid'] = df['close'].rolling(window=20).mean()
        df['BB_std'] = df['close'].rolling(window=20).std()
        bb_upper = df['BB_mid'].iloc[-1] + (2 * df['BB_std'].iloc[-1])
        bb_lower = df['BB_mid'].iloc[-1] - (2 * df['BB_std'].iloc[-1])
        # -----------------------------------------------------------------

        # Nhóm 1: Xu hướng (Trend)
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

        # Nhóm 2: Động lượng (Momentum)
        if rsi > 70: momentum = f"🔥 QUÁ MUA ({rsi:.1f})"
        elif rsi < 30: momentum = f"❄️ QUÁ BÁN ({rsi:.1f})"
        else: momentum = f"⚖️ TRUNG TÍNH ({rsi:.1f})"

        # Nhóm 3: Biến động (Volatility)
        bb_bandwidth = ((bb_upper - bb_lower) / df['BB_mid'].iloc[-1]) * 100
        volatility = "💥 CAO (Mở băng)" if bb_bandwidth > 5 else "💤 THẤP (Bóp băng - Sideway)"

        # Nhóm 4: Khối lượng (Volume)
        avg_vol = df['volume'].iloc[-20:-1].mean()
        last_vol = df['volume'].iloc[-1]
        volume_status = "🐋 ĐỘT BIẾN (Gấp đôi TB)" if last_vol > avg_vol * 2 else "💎 ỔN ĐỊNH"

        # Nhóm 5: Hỗ trợ / Kháng cự
        support = df['low'].rolling(window=30).min().iloc[-1]
        resistance = df['high'].rolling(window=30).max().iloc[-1]

        # --- ĐOẠN ĐỀ XUẤT RISK:REWARD ĐỘNG ĐÃ SỬA ---
        trade_plan = ""
        price_step = last_close * 0.005 # Biên độ nhiễu 0.5%

        if trend_score > 0:
            tp1_target = resistance
            tp2_target = resistance * 1.03
            tp_distance = tp1_target - last_close
            calculated_sl = last_close - (tp_distance / 1.5)
            final_sl = max(calculated_sl, support * 0.995)
            
            trade_plan = (
                "🎯 **Hướng đề xuất: LONG (MUA CƠ CẤU)**\n"
                f" ├ Entry (Hồi nhẹ): `{self.factory.format_price(symbol, last_close - price_step)}`\n"
                f" ├ Stop Loss (SL): `{self.factory.format_price(symbol, final_sl)}` (Tối ưu R:R)\n"
                f" └ Take Profit (TP): TP1: `{self.factory.format_price(symbol, tp1_target)}` | TP2: `{self.factory.format_price(symbol, tp2_target)}`"
            )
        elif trend_score < 0:
            tp1_target = support
            tp2_target = support * 0.97
            tp_distance = last_close - tp1_target
            calculated_sl = last_close + (tp_distance / 1.5)
            final_sl = min(calculated_sl, resistance * 1.005)
            
            trade_plan = (
                "🎯 **Hướng đề xuất: SHORT (BÁN KHỐNG)**\n"
                f" ├ Entry (Hồi xanh): `{self.factory.format_price(symbol, last_close + price_step)}`\n"
                f" ├ Stop Loss (SL): `{self.factory.format_price(symbol, final_sl)}` (Tối ưu R:R)\n"
                f" └ Take Profit (TP): TP1: `{self.factory.format_price(symbol, tp1_target)}` | TP2: `{self.factory.format_price(symbol, tp2_target)}`"
            )
        else:
            trade_plan = (
                "⚠️ **Trạng thái:** Sideway trong biên độ nến.\n"
                f"🟩 **Kịch bản LONG:** Entry `{self.factory.format_price(symbol, support * 1.002)}` | SL `{self.factory.format_price(symbol, support * 0.992)}` | TP `{self.factory.format_price(symbol, last_close)}`\n"
                f"🟥 **Kịch bản SHORT:** Entry `{self.factory.format_price(symbol, resistance * 0.998)}` | SL `{self.factory.format_price(symbol, resistance * 1.008)}` | TP `{self.factory.format_price(symbol, last_close)}`"
            )

        return {
            "trend": trend, "trend_score": trend_score, "rsi": rsi,
            "momentum": momentum, "volatility": volatility, "volume": volume_status,
            "support": support, "resistance": resistance, "close": last_close,
            "trade_plan": trade_plan
        }
    
    def generate_mtf_recommendation(self, symbol):
        intervals = ['15m', '1h', '4h', '1D']
        results = {}
        
        for idx in intervals:
            res = self.analyze_single_frame(symbol, idx)
            if res: results[idx] = res
            
        if not results or '4h' not in results:
            return "❌ Không đủ dữ liệu đa khung để phân tích cặp này."

        total_score = (
            results.get('1D', {}).get('trend_score', 0) * 3 +
            results.get('4h', {}).get('trend_score', 0) * 2.5 +
            results.get('1h', {}).get('trend_score', 0) * 1.5 +
            results.get('15m', {}).get('trend_score', 0) * 1.0
        )
        
        current_price = results['4h']['close']
        sup_4h = results['4h']['support']
        res_4h = results['4h']['resistance']
        
        report = f"📊 **STRATEGY REPORT: {symbol.upper()}**\n"
        report += f"💵 Giá hiện tại: `{self.factory.format_price(symbol, current_price)}`\n"
        report += "-----------------------------------------\n"
        
        report += "🔍 **Cấu trúc xu hướng đa khung:**\n"
        for idx in intervals:
            if idx in results:
                report += f" ├ Khung {idx}: {results[idx]['trend']}\n"
        
        is_sideway = abs(total_score) <= 3.0 or (45 <= results['4h']['rsi'] <= 55)
        
        if is_sideway:
            report += "\n⚠️ **TRẠNG THÁI: MARKET SIDEWAY (Biên độ hẹp)**\n"
            report += "👉 *Chiến lược đề xuất: Range Trading (Đánh theo biên độ)*\n\n"
            
            entry_long = sup_4h * 1.002
            sl_long = sup_4h * 0.99
            tp1_long = current_price
            tp2_long = res_4h * 0.995
            
            entry_short = res_4h * 0.998
            sl_short = res_4h * 1.01
            tp1_short = current_price
            tp2_short = sup_4h * 1.005
            
            report += f"🟩 **Kịch bản LONG (Mua hỗ trợ):**\n"
            report += f" ├ Entry: `{self.factory.format_price(symbol, entry_long)}`\n"
            report += f" ├ SL: `{self.factory.format_price(symbol, sl_long)}`\n"
            report += f" └ TP: `{self.factory.format_price(symbol, tp1_long)}` | `{self.factory.format_price(symbol, tp2_long)}`\n\n"

            report += f"🟥 **Kịch bản SHORT (Bán kháng cự):**\n"
            report += f" ├ Entry: `{self.factory.format_price(symbol, entry_short)}`\n"
            report += f" ├ SL: `{self.factory.format_price(symbol, sl_short)}`\n"
            report += f" └ TP: `{self.factory.format_price(symbol, tp1_short)}` | `{self.factory.format_price(symbol, tp2_short)}`\n"
            
            reason = "Các khung giờ triệt tiêu tín hiệu xu hướng, RSI đi ngang tích lũy chặt chẽ."
            invalidation = f"Nến 4h breakout đóng cửa ngoài biên độ `{self.factory.format_price(symbol, sup_4h)}` hoặc `{self.factory.format_price(symbol, res_4h)}`."
            
        else:
            if total_score > 0:
                direction = "🟢 LONG (MUA)"
                confidence = "🔥 Cao" if total_score > 6 else "⚡ Trung Bình"
                entry = current_price * 0.995
                sl = results['4h']['support'] * 0.995
                tp1 = current_price * 1.02
                tp2 = results['4h']['resistance']
                reason = "Xu hướng đa khung đồng thuận hướng lên, dòng tiền đổ vào mạnh."
                invalidation = f"Giá sập mạnh thủng qua mức hỗ trợ cấu trúc tại `{self.factory.format_price(symbol, sl)}`."
            else:
                direction = "🔴 SHORT (BÁN)"
                confidence = "🔥 Cao" if total_score < -6 else "⚡ Trung Bình"
                entry = current_price * 1.005
                sl = results['4h']['resistance'] * 1.005
                tp1 = current_price * 0.98
                tp2 = results['4h']['support']
                reason = "Áp lực cung đè nặng toàn bộ cấu trúc MTF, EMA tạo giao cắt tử thần."
                invalidation = f"Giá đảo chiều tăng mạnh vượt qua mốc kháng cự cấu trúc `{self.factory.format_price(symbol, sl)}`."

            report += f"\n🎯 **HƯỚNG ĐỀ XUẤT CHÍNH: {direction}**\n"
            report += f" ├ Độ tin cậy: {confidence} (Score: {total_score})\n"
            report += f" ├ Entry đề xuất: `{self.factory.format_price(symbol, entry)}`\n"
            report += f" ├ Stop Loss (SL): `{self.factory.format_price(symbol, sl)}`\n"
            report += f" └ Take Profit: TP1: `{self.factory.format_price(symbol, tp1)}` | TP2: `{self.factory.format_price(symbol, tp2)}`\n"

        report += "\n-----------------------------------------\n"
        report += f"ℹ️ **Lý do giải thích:** {reason}\n"
        report += f"❌ **Điều kiện hủy bỏ lệnh:** {invalidation}\n\n"
        report += "🛡️ **QUẢN TRỊ RỦI RO:**\n"
        report += " └ Tuyệt đối không rủi ro quá 2% tài khoản cho mỗi vị thế trading."
        
        return report

binance_factory = BinanceDataFactory()
analyzer = MarketAnalyzer(binance_factory)

# -------------------------------------------------------------------------
# 3. INTERFACE LAYER: ĐIỀU HƯỚNG MENU TELEGRAM
# -------------------------------------------------------------------------
def validate_and_format_symbol(text: str) -> str:
    symbol = text.strip().upper()
    if not symbol:
        return None
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    if symbol in binance_factory.symbol_info:
        return symbol
    return None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 **Chào mừng bạn đến với Bot Phân Tích Crypto Đa Khung Chuyên Nghiệp!**\n\n"
        "⚡ **Trải nghiệm tối giản hóa:**\n"
        "👉 Nhập trực tiếp tên mã Coin mong muốn vào khung chat (Ví dụ: `btc`, `sol`, `eth`...).\n"
        "Bot sẽ tự động định dạng và mở menu chức năng tương ứng ngay lập tức!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_raw_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    symbol = validate_and_format_symbol(user_text)
    
    if not symbol:
        await update.message.reply_text(
            f"❌ Không tìm thấy cặp giao dịch nào khớp với từ khóa `{user_text.upper()}` trên Binance Futures.\n\n"
            "📌 *Mẹo:* Chỉ cần nhập tên viết tắt như `BTC`, `SOL`, `ETH`, `NEAR`...",
            parse_mode="Markdown"
        )
        return

    await show_coin_main_menu(update, symbol)

async def show_coin_main_menu(update: Update, symbol: str):
    keyboard = [
        [
            InlineKeyboardButton("📊 Phân Tích Đơn Khung", callback_data=f"ask_tf_{symbol}"),
            InlineKeyboardButton("🎯 Chiến Lược Đa Khung (MTF)", callback_data=f"run_rec_{symbol}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = f"✨ **DỮ LIỆU ĐÃ SẴN SÀNG: {symbol}**\n\nChọn một phương thức phân tích bên dưới:"
    
    if update.message:
        await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")

async def ask_timeframe(update: Update, symbol: str):
    keyboard = [
        [
            InlineKeyboardButton("15 Phút (15m)", callback_data=f"run_ana_{symbol}_15m"),
            InlineKeyboardButton("1 Giờ (1h)", callback_data=f"run_ana_{symbol}_1h")
        ],
        [
            InlineKeyboardButton("4 Giờ (4h)", callback_data=f"run_ana_{symbol}_4h"),
            InlineKeyboardButton("1 Ngày (1D)", callback_data=f"run_ana_{symbol}_1D")
        ],
        [
            InlineKeyboardButton("↩️ Quay lại Menu chính", callback_data=f"main_menu_{symbol}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = f"⏱️ Chọn **khung thời gian** cần phân tích kỹ thuật cho cặp `{symbol}`:"
    
    if update.callback_query:
        await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")

async def execute_recommendation(update: Update, symbol: str):
    is_callback = update.callback_query is not None
    target_msg = update.callback_query.message if is_callback else update.message
    
    waiting_msg = await target_msg.reply_text(f"⏳ Hệ thống đang quét cấu trúc đa khung cho `{symbol}`. Vui lòng đợi...")
    
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, analyzer.generate_mtf_recommendation, symbol)
    
    nav_keyboard = [
        [
            InlineKeyboardButton("📊 Xem Phân Tích Đơn Khung", callback_data=f"ask_tf_{symbol}"),
            InlineKeyboardButton("↩️ Menu chính", callback_data=f"main_menu_{symbol}")
        ]
    ]
    await waiting_msg.edit_text(report, reply_markup=InlineKeyboardMarkup(nav_keyboard), parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("main_menu_"):
        symbol = data.replace("main_menu_", "")
        await show_coin_main_menu(update, symbol)
        
    elif data.startswith("ask_tf_"):
        symbol = data.replace("ask_tf_", "")
        await ask_timeframe(update, symbol)
        
    elif data.startswith("run_rec_"):
        symbol = data.replace("run_rec_", "")
        await execute_recommendation(update, symbol)
        
    elif data.startswith("run_ana_"):
        parts = data.replace("run_ana_", "").split("_")
        symbol, interval = parts[0], parts[1]
        
        waiting_msg = await query.message.reply_text(f"⏳ Đang bóc tách chỉ báo `{symbol}` khung `{interval}`...")
        
        loop = asyncio.get_event_loop()
        try:
            res = await loop.run_in_executor(None, analyzer.analyze_single_frame, symbol, interval)
        except Exception as e:
            logger.error(f"Lỗi: {e}")
            await waiting_msg.edit_text("❌ Lỗi cấu trúc API trong quá trình tính toán.")
            return
                
        if not res:
            await waiting_msg.edit_text("❌ Lỗi! Không nhận được phản hồi dữ liệu từ sàn.")
            return

        # Nối chuỗi báo cáo chuẩn sạch kèm KHUYẾN NGHỊ ĐỀ XUẤT THƯƠNG MẠI
        report = (
            f"📈 **PHÂN TÍCH ĐƠN KHUNG: {symbol} [{interval}]**\n"
            f"💵 Giá hiện tại: `{binance_factory.format_price(symbol, res['close'])}`\n"
            f"-----------------------------------------\n"
            f"├── 🗺️ **Xu hướng (Trend):** {res['trend']}\n"
            f"├── ⚡ **Động lượng (RSI):** {res['momentum']}\n"
            f"├── 🌊 **Biến động (Bollinger):** {res['volatility']}\n"
            f"├── 📊 **Khối lượng (Volume):** {res['volume']}\n"
            f"└── 🛡️ **Vùng giá trị (S/R Cứng):**\n"
            f"     ├ Kháng cự: `{binance_factory.format_price(symbol, res['resistance'])}`\n"
            f"     └ Hỗ trợ: `{binance_factory.format_price(symbol, res['support'])}`\n\n"
            f"{res['trade_plan']}"
        )
        
        navigation_keyboard = [
            [
                InlineKeyboardButton("🔄 Đổi Khung Thời Gian", callback_data=f"ask_tf_{symbol}"),
                InlineKeyboardButton("🎯 Xem Chiến Lược Đa Khung", callback_data=f"run_rec_{symbol}")
            ],
            [
                InlineKeyboardButton("↩️ Quay lại Menu chính", callback_data=f"main_menu_{symbol}")
            ]
        ]
        
        await waiting_msg.edit_text(report, reply_markup=InlineKeyboardMarkup(navigation_keyboard), parse_mode="Markdown")

def main():
    if TELEGRAM_BOT_TOKEN == "THAY_TOKEN_TELEGRAM_BOT_CỦA_BẠN_VÀO_ĐÂY" or not TELEGRAM_BOT_TOKEN:
        print("❌ LỖI: Điền Token của bạn vào trường cấu hình để khởi chạy.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_raw_text_input))

    print("🚀 BOT ĐÃ ĐƯỢC CẬP NHẬT ĐỀ XUẤT ĐƠN KHUNG VÀ ĐANG CHẠY...")
    application.run_polling()

if __name__ == "__main__":
    main()