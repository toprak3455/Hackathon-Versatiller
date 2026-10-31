from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta
import urllib.request
import threading
import copy

app = Flask(__name__)

NTFY_TOPIC = "ktu_hydropulse_alarm"
ADMIN_SIFRE = "ktu2026"

AYLAR_TR = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"
]

def tarih_formatla(dt):
    return f"{dt.day} {AYLAR_TR[dt.month]} {dt.year}"

def telefona_bildirim_gonder(baslik, mesaj):
    def gonder():
        try:
            url = f"https://ntfy.sh/{NTFY_TOPIC}"
            req = urllib.request.Request(
                url,
                data=mesaj.encode('utf-8'),
                headers={
                    "Title": baslik.encode('utf-8'),
                    "Priority": "urgent",
                    "Tags": "warning,rotating_light,droplet"
                },
                method="POST"
            )
            urllib.request.urlopen(req, timeout=4)
        except Exception as e:
            print("Bildirim hatasi:", e)
    threading.Thread(target=gonder, daemon=True).start()

binalar = ["YAZILIM", "EHM", "INSAAT"]

# Günlük Sayaçlar (İsraf / Kaçak Su eklendi)
tuketim = {
    b: {k: {"su_harcanan_sn": 0, "su_kurtarilan_sn": 0, "kacak_sn": 0} for k in [1, 2, 3]}
    for b in binalar
}

sensor_durum = {
    b: {k: {"aktif_onceki": 0, "kapali_sn": 0, "kacak_sayac_sn": 0, "kilitli": False} for k in [1, 2, 3]}
    for b in binalar
}

aktif_durum = {"bina": "YAZILIM", "kat": 1, "akis": 0, "el": 0, "kilitli": False}

baslangic_tarihi = datetime(2026, 10, 30)
aktif_gun_index = 0
gecmis_gunler = []

ariza_defteri = []
ariza_sayaci = 1

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/veri', methods=['POST'])
def veri_al():
    global aktif_durum, ariza_sayaci
    veri = request.json
    
    bina = veri.get('bina', 'YAZILIM')
    kat = int(veri.get('kat', 1))
    akis = int(veri.get('akis', veri.get('isik', 0)))
    el = int(veri.get('el', veri.get('su', 0)))

    hucre = sensor_durum[bina][kat]
    kilit_durumu = hucre["kilitli"]

    if kilit_durumu:
        aktif_durum = {"bina": bina, "kat": kat, "akis": akis, "el": el, "kilitli": True}
        return jsonify({"status": "locked", "kilitli": True})

    # Kaçak Tespiti
    if el == 0 and akis == 1:
        hucre["kacak_sayac_sn"] += 1
        tuketim[bina][kat]["kacak_sn"] += 1
        
        if hucre["kacak_sayac_sn"] >= 10:
            hucre["kilitli"] = True
            kilit_durumu = True
            sim_tarih = tarih_formatla(baslangic_tarihi + timedelta(days=aktif_gun_index))
            saat_str = datetime.now().strftime("%H:%M:%S")
            
            yeni_ariza = {
                "id": ariza_sayaci,
                "tarih": f"{sim_tarih} ({saat_str})",
                "bina": bina,
                "kat": kat,
                "sebep": "Musluk kapalıyken 10 sn kesintisiz su akışı (Valf/Tesisat Kaçağı)",
                "durum": "AKTİF ARIZA - MUSLUK KİLİTLİ",
                "cozuldu": False,
                "cozum_zamani": "-"
            }
            ariza_defteri.insert(0, yeni_ariza)
            ariza_sayaci += 1

            telefona_bildirim_gonder(
                "ACIL SU KACAGI ALGILANDI!",
                f"{bina} Binasi {kat}. Kat tuvaletinde musluk kullanilmiyorken 10 sn su akisi tespit edildi! Valf kilitlendi. Ariza No: #{yeni_ariza['id']}"
            )
    else:
        hucre["kacak_sayac_sn"] = 0

    if not kilit_durumu:
        gercek_su_kullanimi = (el == 1 and akis == 1)

        if gercek_su_kullanimi:
            tuketim[bina][kat]["su_harcanan_sn"] += 1
            if hucre["aktif_onceki"] == 0:
                kapali_sure = hucre["kapali_sn"]
                if 0 < kapali_sure <= 7:
                    tuketim[bina][kat]["su_kurtarilan_sn"] += kapali_sure
                hucre["kapali_sn"] = 0
            hucre["aktif_onceki"] = 1
        else:
            if el == 0 and akis == 0:
                hucre["kapali_sn"] += 1
            hucre["aktif_onceki"] = 0

    aktif_durum = {"bina": bina, "kat": kat, "akis": akis, "el": el, "kilitli": kilit_durumu}
    return jsonify({"status": "success", "kilitli": kilit_durumu})

@app.route('/api/durum', methods=['GET'])
def durum_ver():
    suanki_tarih = baslangic_tarihi + timedelta(days=aktif_gun_index)
    aktif_arizalar = [a for a in ariza_defteri if not a["cozuldu"]]
    return jsonify({
        "aktif": aktif_durum,
        "tuketim": tuketim,
        "aktif_tarih": tarih_formatla(suanki_tarih),
        "gecmis": gecmis_gunler,
        "ariza_defteri": ariza_defteri,
        "aktif_ariza_sayisi": len(aktif_arizalar)
    })

@app.route('/api/admin_giris', methods=['POST'])
def admin_giris():
    sifre = request.json.get('sifre', '')
    if sifre == ADMIN_SIFRE:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "mesaj": "Hatalı Admin Şifresi!"}), 401

@app.route('/api/ariza_coz', methods=['POST'])
def ariza_coz():
    veri = request.json
    ariza_id = int(veri.get('id'))
    sifre = veri.get('sifre', '')

    if sifre != ADMIN_SIFRE:
        return jsonify({"status": "error", "mesaj": "Yetkisiz işlem!"}), 403

    for a in ariza_defteri:
        if a["id"] == ariza_id and not a["cozuldu"]:
            a["cozuldu"] = True
            a["durum"] = "ÇÖZÜLDÜ (Admin Onaylı)"
            a["cozum_zamani"] = datetime.now().strftime("%H:%M:%S")
            
            b = a["bina"]
            k = a["kat"]
            baska_aktif_var_mi = any(
                x["bina"] == b and x["kat"] == k and not x["cozuldu"] for x in ariza_defteri
            )
            if not baska_aktif_var_mi:
                sensor_durum[b][k]["kilitli"] = False
                sensor_durum[b][k]["kacak_sayac_sn"] = 0
            break

    return jsonify({"status": "success"})

@app.route('/api/gun_atla', methods=['POST'])
def gun_atla():
    global tuketim, aktif_gun_index, gecmis_gunler, sensor_durum
    
    biten_tarih = baslangic_tarihi + timedelta(days=aktif_gun_index)
    
    gecmis_gunler.append({
        "tarih": tarih_formatla(biten_tarih),
        "tuketim": copy.deepcopy(tuketim)
    })

    aktif_gun_index += 1
    for b in binalar:
        for k in [1, 2, 3]:
            tuketim[b][k]["su_harcanan_sn"] = 0
            tuketim[b][k]["su_kurtarilan_sn"] = 0
            tuketim[b][k]["kacak_sn"] = 0
            sensor_durum[b][k]["aktif_onceki"] = 0
            sensor_durum[b][k]["kapali_sn"] = 0

    return jsonify({"status": "success"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)