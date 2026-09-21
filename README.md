 projek ini masih akses awal jadi mohon maklum kan.

cara memasang sript



# 1. Update paket Termux
pkg update -y && pkg upgrade -y

# 2. Install Python, git, dan Termux:API (untuk wake lock)
pkg install -y python git termux-api

# 3. Clone repository (ganti URL sesuai repo GitHub kamu)
git clone https://github.com/<username>/ai-proxy.git
cd ai-proxy

# 4. Install dependency Python
pip install -r requirements.txt

# 5. Jalankan proxy
python proxy.py