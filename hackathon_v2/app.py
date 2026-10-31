# -*- coding: utf-8 -*-
"""
KAMPÜS IoT SU, ENERJİ VE KAÇAK YÖNETİMİ PLATFORMU
UI GreenMetric & YÖK Sürdürülebilirlik Muhasebecisi — Flask arka ucu

Çalıştırma
    pip install -r requirements.txt
    python app.py                       ->  http://127.0.0.1:5000

ESP32 (veya herhangi bir IoT düğümü) uç noktası
    POST /api/sensor
    {"bina": "YAZILIM", "kat": 2, "el": 1, "akis": 1, "debi_lpm": 5.8}
    ->  {"ok": true, "kilitli": false, "valf": "ACIK", ...}

Mantık özeti
    • el == 1 ve akis == 1          -> meşru kullanım: yalnızca harcanan su artar
    • Kurtarılan su                 -> SADECE şu durumda artar: musluk kapandıktan sonra 5 sn içinde
                                       yeniden açılırsa (ör. el sabunlama için çekildi), aradaki kapalı
                                       süre × musluk debisi kadar su kurtarılmış sayılır. Ara 5 sn'yi
                                       geçerse hiçbir şey sayılmaz.
    • el == 1 ve akis == 0          -> musluk açık ama su akmıyor: hiçbir sayaç artmaz
    • el == 0 ve akis == 1          -> israf; 10 sn kesintisiz sürerse "Valf / Tesisat Kaçağı"
                                       -> kat kilitlenir + ntfy push bildirimi + arıza defteri kaydı
    • Kilitli kat                   -> solenoid valf kapalı; el uzatılsa bile su verilmez
    • Kilit yalnızca Admin "Gider & Aç" onayı ile açılır (şifre: ADMIN_SIFRE)
"""
from __future__ import annotations

import atexit
import csv
import hmac
import io
import json
import logging
import os
import random
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from urllib import request as urlrequest

from flask import Flask, Response, jsonify, render_template, request, session

# ══════════════════════════════ YAPILANDIRMA ══════════════════════════════
TEMEL_DIZIN = os.path.dirname(os.path.abspath(__file__))

KURUM_ADI = os.environ.get("KURUM_ADI", "Karadeniz Teknik Üniversitesi")
ADMIN_SIFRE = os.environ.get("ADMIN_SIFRE", "ktu2026")
BASLANGIC_TARIHI = date.fromisoformat(os.environ.get("BASLANGIC_TARIHI", "2026-10-30"))
VERI_DOSYASI = os.environ.get("VERI_DOSYASI", os.path.join(TEMEL_DIZIN, "data", "durum.json"))

# ntfy.sh mobil bildirim — telefonda ntfy uygulamasından NTFY_KONU'ya abone olun.
# Konu adları herkese açıktır; tahmin edilmesi zor, kuruma özel bir ad seçin.
NTFY_AKTIF = os.environ.get("NTFY_AKTIF", "1") == "1"
NTFY_SUNUCU = os.environ.get("NTFY_SUNUCU", "https://ntfy.sh")
NTFY_KONU = os.environ.get("NTFY_KONU", "ktu-kampus-su-alarm")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")          # kendi sunucunuz için isteğe bağlı
PANEL_URL = os.environ.get("PANEL_URL", "")            # bildirime tıklanınca açılacak panel adresi

BINALAR = {
    "YAZILIM": "Yazılım Mühendisliği",
    "EHM": "Elektrik-Elektronik Mühendisliği",
    "INSAAT": "İnşaat Mühendisliği",
}
KATLAR = (1, 2, 3)

# Ölçüm ve muhasebe katsayıları (kurumunuzun değerleriyle güncelleyin)
KACAK_ESIK_SN = 10.0          # el yokken kesintisiz akış süresi -> kaçak
KURTARMA_PENCERE_SN = 5.0     # kapat -> yeniden aç arası en fazla bu kadar olursa kapalı süre kurtarılır
SENSORLU_DEBI_LS = 0.10       # ölçülmüş debi gelmezse kullanılan musluk debisi (L/s) ≈ 6 L/dk
POMPA_KWH_M3 = 0.55           # bina hidrofor/pompa özgül enerjisi (kWh/m³)
SEBEKE_EMISYON = 0.442        # şebeke elektriği emisyon faktörü (kg CO2/kWh) — yaklaşık
SU_TEMIN_KARBON = 0.149       # şebeke suyu temin + arıtma karbonu (kg CO2e/m³) — yaklaşık
AGAC_KG_YIL = 21.0            # olgun bir ağacın yıllık CO2 tutumu (kg)

# Motor ve simülasyon
TICK_SN = 0.5
KAYIT_ARALIGI_SN = 15
ESP32_ZAMAN_ASIMI_SN = 15     # gerçek cihazdan bu süre veri gelmezse kat simülasyona döner
PANEL_ZAMAN_ASIMI_SN = 180    # paneldeki manuel kontrol bu süre sonra otomatiğe döner
SIM_KULLANIM_OLASILIGI = 0.06 # kat başına saniyede kullanım başlama olasılığı
SIM_KACAK_OLASILIGI = 0.00004 # kat başına saniyede rastgele kaçak olasılığı (kampüste ~45 dk'da bir)

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
         "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
TR_ASCII = str.maketrans({"İ": "I", "Ş": "S", "Ğ": "G", "Ü": "U", "Ö": "O", "Ç": "C"})

log = logging.getLogger("kampus-su")


# ══════════════════════════════ YARDIMCILAR ══════════════════════════════
def tarih_etiketi(t: date) -> str:
    return f"{t.day} {AYLAR[t.month - 1]} {t.year} {GUNLER[t.weekday()]}"


def kisa_tarih(t: date) -> str:
    return t.strftime("%d.%m.%Y")


def turet(harcanan: float, kurtarilan: float, israf: float) -> dict:
    """Ham litre değerlerinden enerji, karbon ve ağaç metaforlarını türetir."""
    karbon_m3 = POMPA_KWH_M3 * SEBEKE_EMISYON + SU_TEMIN_KARBON
    pompalanan_m3 = (harcanan + israf) / 1000.0
    co2_onlenen = kurtarilan / 1000.0 * karbon_m3
    toplam = harcanan + israf
    return {
        "harcanan": round(harcanan, 3),
        "kurtarilan": round(kurtarilan, 3),
        "israf": round(israf, 3),
        "enerji": round(pompalanan_m3 * POMPA_KWH_M3, 5),
        "tasarruf_enerji": round(kurtarilan / 1000.0 * POMPA_KWH_M3, 5),
        "co2_onlenen": round(co2_onlenen, 5),
        "co2_kayip": round(israf / 1000.0 * karbon_m3, 5),
        "agac_gun": round(co2_onlenen / (AGAC_KG_YIL / 365.0), 3),
        "israf_orani": round(israf / toplam * 100.0, 2) if toplam > 0 else 0.0,
    }


def ntfy_gonder(ariza: dict) -> str:
    """ntfy.sh JSON yayın API'si ile acil push bildirimi gönderir (Türkçe karakter güvenli)."""
    if not NTFY_AKTIF or not NTFY_KONU:
        return "Devre dışı"
    yuk = {
        "topic": NTFY_KONU,
        "title": f"ACİL: Su kaçağı — {ariza['bina']} Kat {ariza['kat']}",
        "message": (
            f"{ariza['kod']} · {tarih_etiketi(date.fromisoformat(ariza['tarih']))} {ariza['saat']}\n"
            f"El sensörü pasifken {KACAK_ESIK_SN:.0f} sn kesintisiz akış algılandı. "
            f"Kat otomatik kilitlendi. Tespit süresince israf: {ariza['israf']:.2f} L.\n"
            "Arıza giderildikten sonra panelden Admin onayı ile kilit açılabilir."
        ),
        "priority": 5,
        "tags": ["rotating_light", "droplet", "lock"],
    }
    if PANEL_URL:
        yuk["click"] = PANEL_URL
    basliklar = {"Content-Type": "application/json"}
    if NTFY_TOKEN:
        basliklar["Authorization"] = f"Bearer {NTFY_TOKEN}"
    try:
        istek = urlrequest.Request(
            NTFY_SUNUCU.rstrip("/") + "/",
            data=json.dumps(yuk, ensure_ascii=False).encode("utf-8"),
            headers=basliklar,
            method="POST",
        )
        with urlrequest.urlopen(istek, timeout=8) as yanit:
            return "Gönderildi" if 200 <= yanit.status < 300 else f"Hata ({yanit.status})"
    except Exception as hata:  # ağ yok, DNS, zaman aşımı...
        log.warning("ntfy bildirimi gönderilemedi: %s", hata)
        return "Gönderilemedi"


# ══════════════════════════════ ÇEKİRDEK ══════════════════════════════
class Platform:
    """Kampüsün tüm canlı durumunu, günlük sayaçları, arşivi ve arıza defterini tutar."""

    def __init__(self) -> None:
        self.kilit = threading.RLock()
        self.aktif_tarih: date = BASLANGIC_TARIHI
        self.gun_no = 1
        self.katlar = {b: {k: self._yeni_kat() for k in KATLAR} for b in BINALAR}
        self.gunluk = self._bos_gunluk()
        self.gecmis: list[dict] = []
        self.arizalar: list[dict] = []
        self.ariza_sayac = 0
        self.sim_aktif = True
        self.son_konum = (next(iter(BINALAR)), KATLAR[0])
        self._son_kayit = 0.0
        self._yukle()

    # ── yapı taşları ──
    @staticmethod
    def _yeni_kat() -> dict:
        return {
            "el": 0, "akis": 0, "debi": None, "kaynak": "SIM",
            "son_veri": 0.0, "son_degisim": 0.0,
            "kilitli": False, "aktif_ariza": None,
            "kacak_sn": 0.0, "kacak_israf": 0.0,
            # kurtarma aralığı takibi: kullanım bitti -> yeniden açılana kadar geçen süre
            "kullanimda": False, "son_debi": SENSORLU_DEBI_LS,
            "aralik_aktif": False, "aralik_sn": 0.0, "aralik_kapali_sn": 0.0,
            "sim_mod": "bos", "sim_kalan": 0.0, "sim_sonraki": [],
        }

    @staticmethod
    def _aralik_sifirla(kat: dict) -> None:
        kat.update(aralik_aktif=False, aralik_sn=0.0, aralik_kapali_sn=0.0)

    @staticmethod
    def _yeni_sayac() -> dict:
        return {"harcanan": 0.0, "kurtarilan": 0.0, "israf": 0.0, "kullanim_sn": 0.0,
                "kacak_sayisi": 0, "kurtarma_sayisi": 0}

    def _bos_gunluk(self) -> dict:
        return {b: {k: self._yeni_sayac() for k in KATLAR} for b in BINALAR}

    def _sensor_ata(self, b: str, k: int, el: int, akis: int, kaynak: str, debi=None) -> None:
        kat = self.katlar[b][k]
        simdi = time.time()
        if kat["el"] != el or kat["akis"] != akis:
            kat["son_degisim"] = simdi
            if el or akis:
                self.son_konum = (b, k)
        kat.update(el=el, akis=akis, kaynak=kaynak, debi=debi, son_veri=simdi)

    def _simulasyona_don(self, b: str, k: int) -> None:
        self.katlar[b][k].update(sim_mod="bos", sim_kalan=0.0, sim_sonraki=[])
        self._sensor_ata(b, k, 0, 0, "SIM")

    # ── dış girişler ──
    def sensor_guncelle(self, b: str, k: int, el: int, akis: int, kaynak: str = "ESP32", debi_lpm=None) -> dict:
        with self.kilit:
            debi = debi_lpm / 60.0 if debi_lpm else None
            self._sensor_ata(b, k, el, akis, kaynak, debi)
            return self._kat_durum(b, k)

    def kacak_senaryosu(self, b: str, k: int) -> dict:
        """Panel testi: el yok + akış var durumunu tutar; 10 sn sonra kat kilitlenir."""
        with self.kilit:
            self._sensor_ata(b, k, 0, 1, "PANEL")
            return self._kat_durum(b, k)

    def panel_birak(self, b: str, k: int) -> dict:
        with self.kilit:
            self._simulasyona_don(b, k)
            return self._kat_durum(b, k)

    def sim_ayarla(self, aktif: bool) -> None:
        with self.kilit:
            self.sim_aktif = aktif
            self.kaydet()

    # ── motor adımı ──
    def tick(self, dt: float) -> None:
        with self.kilit:
            simdi = time.time()
            for b in BINALAR:
                for k in KATLAR:
                    kat = self.katlar[b][k]
                    gecen = simdi - kat["son_veri"]
                    if (kat["kaynak"] == "ESP32" and gecen > ESP32_ZAMAN_ASIMI_SN) or \
                       (kat["kaynak"] == "PANEL" and gecen > PANEL_ZAMAN_ASIMI_SN):
                        self._simulasyona_don(b, k)
                    if kat["kaynak"] == "SIM":
                        self._sim_adim(b, k, kat, dt)
                    self._olcum_isle(b, k, kat, dt)
            if simdi - self._son_kayit > KAYIT_ARALIGI_SN:
                self.kaydet()

    def _sim_adim(self, b: str, k: int, kat: dict, dt: float) -> None:
        """El yıkama döngüsü: yıka -> (çoğu zaman) sabunla, musluk kapalı -> durula.
        Sabunlama arası bazen 5 sn'nin altında (kurtarılır), bazen üstünde (sayılmaz)."""
        if not self.sim_aktif or kat["kilitli"]:
            kat.update(sim_mod="bos", sim_sonraki=[])
            self._sensor_ata(b, k, 0, 0, "SIM")
            return
        if kat["sim_mod"] == "kacak":  # kaçak senaryosu kilitlenene kadar sürer
            self._sensor_ata(b, k, 0, 1, "SIM")
            return
        if kat["sim_mod"] == "bos":
            r = random.random()
            if r < SIM_KACAK_OLASILIGI * dt:
                kat["sim_mod"] = "kacak"
                self._sensor_ata(b, k, 0, 1, "SIM")
                return
            if r >= (SIM_KACAK_OLASILIGI + SIM_KULLANIM_OLASILIGI) * dt:
                self._sensor_ata(b, k, 0, 0, "SIM")
                return
            # yeni el yıkama: [mod, süre] adımları
            adimlar = [["yika", random.uniform(2.0, 4.0)]]
            if random.random() < 0.7:
                adimlar += [["sabun", random.uniform(2.0, 7.0)], ["yika", random.uniform(3.0, 6.0)]]
            kat["debi"] = random.uniform(0.085, 0.11)
            kat["sim_sonraki"] = adimlar
            kat["sim_mod"], kat["sim_kalan"] = kat["sim_sonraki"].pop(0)
        else:
            kat["sim_kalan"] -= dt
            if kat["sim_kalan"] <= 0:
                if kat["sim_sonraki"]:
                    kat["sim_mod"], kat["sim_kalan"] = kat["sim_sonraki"].pop(0)
                else:
                    kat["sim_mod"] = "bos"
        if kat["sim_mod"] == "yika":
            self._sensor_ata(b, k, 1, 1, "SIM", kat["debi"])
        else:  # sabunlama arası veya boşta: musluk kapalı
            self._sensor_ata(b, k, 0, 0, "SIM", kat["debi"] if kat["sim_mod"] == "sabun" else None)

    def _olcum_isle(self, b: str, k: int, kat: dict, dt: float) -> None:
        sayac = self.gunluk[b][k]
        if kat["kilitli"]:                       # solenoid valf kapalı -> su yok
            kat.update(kacak_sn=0.0, kacak_israf=0.0, kullanimda=False)
            self._aralik_sifirla(kat)
            return
        q = kat["debi"] or SENSORLU_DEBI_LS
        if kat["el"] and kat["akis"]:            # çapraz doğrulanmış meşru kullanım
            if kat["aralik_aktif"]:              # kapandıktan sonra yeniden açıldı
                if kat["aralik_sn"] <= KURTARMA_PENCERE_SN and kat["aralik_kapali_sn"] > 0:
                    sayac["kurtarilan"] += kat["aralik_kapali_sn"] * kat["son_debi"]
                    sayac["kurtarma_sayisi"] += 1
                self._aralik_sifirla(kat)
            sayac["harcanan"] += q * dt
            sayac["kullanim_sn"] += dt
            kat.update(kullanimda=True, son_debi=q, kacak_sn=0.0, kacak_israf=0.0)
            return

        if kat["kullanimda"]:                    # kullanım az önce bitti -> kurtarma aralığı başlar
            kat["kullanimda"] = False
            kat.update(aralik_aktif=True, aralik_sn=0.0, aralik_kapali_sn=0.0)
        if kat["aralik_aktif"]:
            kat["aralik_sn"] += dt
            if not kat["el"] and not kat["akis"]:  # yalnızca gerçekten kapalı geçen süre
                kat["aralik_kapali_sn"] += dt
            if kat["aralik_sn"] > KURTARMA_PENCERE_SN:
                self._aralik_sifirla(kat)        # 5 sn geçti -> bu aralık sayılmaz

        if kat["akis"] and not kat["el"]:        # el yok ama su akıyor -> israf
            kat["kacak_sn"] += dt
            kat["kacak_israf"] += q * dt
            sayac["israf"] += q * dt
            if kat["kacak_sn"] >= KACAK_ESIK_SN:
                self._kilitle(b, k, kat)
        else:                                    # boşta veya musluk açık/akış yok
            kat["kacak_sn"] = kat["kacak_israf"] = 0.0

    def _kilitle(self, b: str, k: int, kat: dict) -> None:
        self.ariza_sayac += 1
        simdi = datetime.now()
        ariza = {
            "id": self.ariza_sayac,
            "kod": f"ARZ-{self.ariza_sayac:04d}",
            "tarih": self.aktif_tarih.isoformat(),
            "saat": simdi.strftime("%H:%M:%S"),
            "bina": b,
            "kat": k,
            "tip": "Valf / Tesisat Kaçağı",
            "aciklama": f"El sensörü pasifken {KACAK_ESIK_SN:.0f} sn kesintisiz akış algılandı; kat otomatik kilitlendi.",
            "israf": round(kat["kacak_israf"], 3),
            "durum": "AKTIF",
            "bildirim": "Gönderiliyor",
            "cozum_tarih": None,
            "cozum_saat": None,
            "onaylayan": None,
        }
        self.arizalar.append(ariza)
        kat.update(kilitli=True, aktif_ariza=ariza["id"], kacak_sn=0.0, kacak_israf=0.0)
        self.gunluk[b][k]["kacak_sayisi"] += 1
        if kat["kaynak"] in ("SIM", "PANEL"):    # valf kapandı -> akış sensörü 0 okur
            kat.update(akis=0, sim_mod="bos", sim_sonraki=[])
        log.warning("KAÇAK: %s %s Kat %s kilitlendi (%.2f L)", ariza["kod"], b, k, ariza["israf"])
        self.kaydet()
        threading.Thread(target=self._bildirim_gonder, args=(dict(ariza),), daemon=True).start()

    def _bildirim_gonder(self, ariza: dict) -> None:
        sonuc = ntfy_gonder(ariza)
        with self.kilit:
            for a in self.arizalar:
                if a["id"] == ariza["id"]:
                    a["bildirim"] = sonuc
            self.kaydet()

    # ── yönetici işlemleri ──
    def ariza_coz(self, ariza_id: int, onaylayan: str = "Admin"):
        with self.kilit:
            a = next((x for x in self.arizalar if x["id"] == ariza_id), None)
            if a is None:
                return None, "Arıza kaydı bulunamadı."
            if a["durum"] != "AKTIF":
                return None, "Bu arıza zaten çözülmüş."
            a.update(durum="COZULDU", cozum_tarih=self.aktif_tarih.isoformat(),
                     cozum_saat=datetime.now().strftime("%H:%M:%S"), onaylayan=onaylayan)
            b, k = a["bina"], a["kat"]
            kat = self.katlar[b][k]
            kat.update(kilitli=False, aktif_ariza=None, kacak_sn=0.0, kacak_israf=0.0)
            if kat["kaynak"] in ("SIM", "PANEL"):
                self._simulasyona_don(b, k)
            self.kaydet()
            return dict(a), None

    def gunu_bitir(self) -> dict:
        with self.kilit:
            ozet = self.gun_ozeti(canli=False)
            self.gecmis.append(ozet)
            self.gunluk = self._bos_gunluk()
            self.aktif_tarih += timedelta(days=1)
            self.gun_no += 1
            self.kaydet()
            return ozet

    # ── okuma / özet ──
    def gun_ozeti(self, canli: bool) -> dict:
        binalar, kampus = {}, self._yeni_sayac()
        for b in BINALAR:
            bina_top, katlar = self._yeni_sayac(), {}
            for k in KATLAR:
                s = self.gunluk[b][k]
                katlar[str(k)] = {
                    "harcanan": round(s["harcanan"], 3), "kurtarilan": round(s["kurtarilan"], 3),
                    "israf": round(s["israf"], 3), "kullanim_sn": round(s["kullanim_sn"], 1),
                    "kacak_sayisi": s["kacak_sayisi"], "kurtarma_sayisi": s["kurtarma_sayisi"],
                }
                for anahtar in bina_top:
                    bina_top[anahtar] += s[anahtar]
            for anahtar in kampus:
                kampus[anahtar] += bina_top[anahtar]
            binalar[b] = {
                **turet(bina_top["harcanan"], bina_top["kurtarilan"], bina_top["israf"]),
                "kullanim_sn": round(bina_top["kullanim_sn"], 1),
                "kacak_sayisi": bina_top["kacak_sayisi"],
                "kurtarma_sayisi": bina_top["kurtarma_sayisi"],
                "katlar": katlar,
            }
        return {
            "tarih": self.aktif_tarih.isoformat(),
            "etiket": tarih_etiketi(self.aktif_tarih),
            "kisa": kisa_tarih(self.aktif_tarih),
            "gun_no": self.gun_no,
            "canli": canli,
            "binalar": binalar,
            "toplam": {
                **turet(kampus["harcanan"], kampus["kurtarilan"], kampus["israf"]),
                "kullanim_sn": round(kampus["kullanim_sn"], 1),
                "kacak_sayisi": kampus["kacak_sayisi"],
                "kurtarma_sayisi": kampus["kurtarma_sayisi"],
            },
        }

    def _kat_durum(self, b: str, k: int) -> dict:
        kat = self.katlar[b][k]
        if kat["kilitli"]:
            durum = "KILITLI"
        elif kat["akis"] and not kat["el"]:
            durum = "KACAK"
        elif kat["el"] and kat["akis"]:
            durum = "KULLANIM"
        elif kat["el"]:
            durum = "AKIS_YOK"
        elif kat["aralik_aktif"]:
            durum = "ARALIK"                     # kapandı, 5 sn içinde yeniden açılması bekleniyor
        else:
            durum = "BOSTA"
        aralik = kat["aralik_aktif"] and not kat["kilitli"]
        return {
            "bina": b, "kat": k, "el": kat["el"], "akis": kat["akis"], "durum": durum,
            "kilitli": kat["kilitli"], "valf": "KAPALI" if kat["kilitli"] else "ACIK",
            "kacak_sn": round(kat["kacak_sn"], 1), "kaynak": kat["kaynak"],
            "aralik_sn": round(kat["aralik_sn"], 1) if aralik else None,
            "bekleyen_kurtarma": round(kat["aralik_kapali_sn"] * kat["son_debi"], 3) if aralik else None,
            "debi_lpm": round(kat["debi"] * 60, 2) if kat["debi"] and kat["akis"] else None,
            "ariza_kodu": f"ARZ-{kat['aktif_ariza']:04d}" if kat["aktif_ariza"] else None,
        }

    def _telemetri(self) -> dict:
        hepsi = [(b, k, self.katlar[b][k]) for b in BINALAR for k in KATLAR]
        kacak = [x for x in hepsi if x[2]["kacak_sn"] > 0 and not x[2]["kilitli"]]
        if kacak:
            b, k, _ = max(kacak, key=lambda x: x[2]["kacak_sn"])
        else:
            aktif = [x for x in hepsi if x[2]["el"] or x[2]["akis"] or (x[2]["aralik_aktif"] and not x[2]["kilitli"])]
            b, k = (max(aktif, key=lambda x: x[2]["son_degisim"])[:2] if aktif else self.son_konum)
        return self._kat_durum(b, k)

    def durum(self, admin: bool = False) -> dict:
        with self.kilit:
            katlar = {b: [self._kat_durum(b, k) for k in KATLAR] for b in BINALAR}
            tum = [x for liste in katlar.values() for x in liste]
            return {
                "kurum": KURUM_ADI,
                "baslangic": BASLANGIC_TARIHI.isoformat(),
                "tarih": self.aktif_tarih.isoformat(),
                "tarih_etiket": tarih_etiketi(self.aktif_tarih),
                "gun_no": self.gun_no,
                "sunucu_saati": datetime.now().strftime("%H:%M:%S"),
                "sim_aktif": self.sim_aktif,
                "admin": admin,
                "gecmis_surum": len(self.gecmis),
                "kacak_esik": KACAK_ESIK_SN,
                "binalar": [{"kod": b, "ad": ad, "katlar": katlar[b]} for b, ad in BINALAR.items()],
                "bugun": self.gun_ozeti(canli=True),
                "telemetri": self._telemetri(),
                "ozet": {
                    "kullanimda": sum(x["durum"] == "KULLANIM" for x in tum),
                    "kacak": sum(x["durum"] == "KACAK" for x in tum),
                    "kilitli": sum(x["kilitli"] for x in tum),
                    "esp32": sum(x["kaynak"] == "ESP32" for x in tum),
                    "panel": sum(x["kaynak"] == "PANEL" for x in tum),
                    "sim": sum(x["kaynak"] == "SIM" for x in tum),
                    "aktif_ariza": sum(a["durum"] == "AKTIF" for a in self.arizalar),
                },
                "arizalar": [dict(a) for a in reversed(self.arizalar[-500:])],
                "kurtarma_pencere": KURTARMA_PENCERE_SN,
                "katsayilar": {
                    "sensorlu_lpm": SENSORLU_DEBI_LS * 60, "kurtarma_pencere_sn": KURTARMA_PENCERE_SN,
                    "pompa_kwh_m3": POMPA_KWH_M3, "sebeke_emisyon": SEBEKE_EMISYON,
                    "su_temin_karbon": SU_TEMIN_KARBON, "agac_kg_yil": AGAC_KG_YIL,
                    "kacak_esik_sn": KACAK_ESIK_SN, "ntfy_konu": NTFY_KONU if NTFY_AKTIF else None,
                },
            }

    def gecmis_al(self) -> list[dict]:
        with self.kilit:
            return list(self.gecmis)

    # ── Excel (CSV) raporu ──
    def csv_rapor(self) -> str:
        with self.kilit:
            gunler = list(self.gecmis) + [self.gun_ozeti(canli=True)]
            arizalar = [dict(a) for a in self.arizalar]

        def s(x, n=2):  # Türkçe Excel: ondalık virgül
            return f"{x:.{n}f}".replace(".", ",")

        out = io.StringIO()
        w = csv.writer(out, delimiter=";", lineterminator="\r\n")
        w.writerow(["KAMPÜS IoT SU, ENERJİ VE KAÇAK YÖNETİMİ — DETAYLI SÜRDÜRÜLEBİLİRLİK RAPORU"])
        w.writerow(["Kurum", KURUM_ADI])
        w.writerow(["Raporlama Çerçevesi", "UI GreenMetric (WR: Su, EC: Enerji ve İklim Değişikliği) · YÖK Sürdürülebilirlik Raporlaması"])
        w.writerow(["Rapor Oluşturma Zamanı", datetime.now().strftime("%d.%m.%Y %H:%M:%S")])
        w.writerow(["Raporlama Dönemi", f"{gunler[0]['kisa']} – {gunler[-1]['kisa']}", f"{len(gunler)} gün (son gün açık/canlı)"])
        w.writerow(["İzlenen Binalar", " · ".join(f"{k} ({v})" for k, v in BINALAR.items())])
        w.writerow([])

        w.writerow(["1) GÜNLÜK BİNA BAZLI SU, İSRAF VE ENERJİ VERİLERİ"])
        w.writerow(["Tarih", "Gün No", "Kayıt Durumu", "Bina Kodu", "Bina Adı", "Harcanan Su (L)",
                    "Kurtarılan Su (L)", "İsraf / Kaçak Su (L)", "İsraf Oranı (%)", "Pompa Enerjisi (kWh)",
                    "Tasarruf Edilen Enerji (kWh)", "Önlenen CO2e (kg)", "Kaçak Kaynaklı CO2e (kg)",
                    "Ağaç-Gün Eşdeğeri", "Kullanım Süresi (sn)", "Kaçak Olayı (adet)",
                    "Kurtarma Olayı (adet)"])
        for g in gunler:
            durum = "Açık gün (canlı)" if g["canli"] else "Arşivlendi"
            satirlar = [(b, BINALAR[b], g["binalar"][b]) for b in BINALAR] + [("KAMPÜS", "Kampüs Toplamı", g["toplam"])]
            for kod, ad, m in satirlar:
                w.writerow([g["kisa"], g["gun_no"], durum, kod, ad, s(m["harcanan"]), s(m["kurtarilan"]),
                            s(m["israf"]), s(m["israf_orani"]), s(m["enerji"], 4), s(m["tasarruf_enerji"], 4),
                            s(m["co2_onlenen"], 4), s(m["co2_kayip"], 4), s(m["agac_gun"], 2),
                            s(m["kullanim_sn"], 1), m["kacak_sayisi"], m.get("kurtarma_sayisi", 0)])
        w.writerow([])

        w.writerow(["2) GÜNLÜK KAT BAZLI DETAY"])
        w.writerow(["Tarih", "Gün No", "Bina Kodu", "Kat", "Harcanan Su (L)", "Kurtarılan Su (L)",
                    "İsraf / Kaçak Su (L)", "Pompa Enerjisi (kWh)", "Kullanım Süresi (sn)", "Kaçak Olayı (adet)",
                    "Kurtarma Olayı (adet)"])
        for g in gunler:
            for b in BINALAR:
                for k, m in g["binalar"][b]["katlar"].items():
                    w.writerow([g["kisa"], g["gun_no"], b, k, s(m["harcanan"]), s(m["kurtarilan"]), s(m["israf"]),
                                s((m["harcanan"] + m["israf"]) / 1000 * POMPA_KWH_M3, 4),
                                s(m["kullanim_sn"], 1), m["kacak_sayisi"], m.get("kurtarma_sayisi", 0)])
        w.writerow([])

        w.writerow(["3) DÖNEM TOPLAMLARI (BİNA BAZLI)"])
        w.writerow(["Bina Kodu", "Bina Adı", "Harcanan Su (L)", "Kurtarılan Su (L)", "İsraf / Kaçak Su (L)",
                    "İsraf Oranı (%)", "Pompa Enerjisi (kWh)", "Tasarruf Edilen Enerji (kWh)",
                    "Önlenen CO2e (kg)", "Ağaç-Gün Eşdeğeri", "Kaçak Olayı (adet)", "Kurtarma Olayı (adet)"])
        genel = {"harcanan": 0.0, "kurtarilan": 0.0, "israf": 0.0, "kacak": 0, "kurtarma": 0}
        for b, ad in list(BINALAR.items()) + [("KAMPÜS", "Kampüs Toplamı")]:
            if b == "KAMPÜS":
                h, k_, i = genel["harcanan"], genel["kurtarilan"], genel["israf"]
                kc, ku = genel["kacak"], genel["kurtarma"]
            else:
                h = sum(g["binalar"][b]["harcanan"] for g in gunler)
                k_ = sum(g["binalar"][b]["kurtarilan"] for g in gunler)
                i = sum(g["binalar"][b]["israf"] for g in gunler)
                kc = sum(g["binalar"][b]["kacak_sayisi"] for g in gunler)
                ku = sum(g["binalar"][b].get("kurtarma_sayisi", 0) for g in gunler)
                genel["harcanan"] += h; genel["kurtarilan"] += k_; genel["israf"] += i
                genel["kacak"] += kc; genel["kurtarma"] += ku
            m = turet(h, k_, i)
            w.writerow([b, ad, s(m["harcanan"]), s(m["kurtarilan"]), s(m["israf"]), s(m["israf_orani"]),
                        s(m["enerji"], 4), s(m["tasarruf_enerji"], 4), s(m["co2_onlenen"], 4),
                        s(m["agac_gun"], 2), kc, ku])
        w.writerow([])

        w.writerow(["4) ARIZA DEFTERİ (TÜM KAYITLAR)"])
        w.writerow(["Arıza Kodu", "Tespit Tarihi", "Tespit Saati", "Bina", "Kat", "Arıza Tipi", "Açıklama",
                    "Tespit Süresince İsraf (L)", "Durum", "Mobil Bildirim (ntfy)", "Çözüm Tarihi",
                    "Çözüm Saati", "Onaylayan"])
        if not arizalar:
            w.writerow(["Kayıtlı arıza yok"])
        for a in arizalar:
            w.writerow([a["kod"], kisa_tarih(date.fromisoformat(a["tarih"])), a["saat"], a["bina"], a["kat"],
                        a["tip"], a["aciklama"], s(a["israf"]),
                        "Aktif (kat kilitli)" if a["durum"] == "AKTIF" else "Çözüldü (kilit açıldı)",
                        a.get("bildirim") or "-",
                        kisa_tarih(date.fromisoformat(a["cozum_tarih"])) if a.get("cozum_tarih") else "-",
                        a.get("cozum_saat") or "-", a.get("onaylayan") or "-"])
        w.writerow([])

        w.writerow(["5) METODOLOJİ VE KATSAYILAR"])
        for satir in [
            ["Çapraz doğrulama", "Tüketim yalnızca el sensörü = 1 VE akış sensörü = 1 iken sayılır."],
            ["Kaçak kuralı", f"El sensörü = 0 iken {KACAK_ESIK_SN:.0f} sn kesintisiz akış -> Valf / Tesisat Kaçağı, kat kilidi."],
            ["Kurtarılan su", f"Yalnızca musluk kapandıktan sonra {KURTARMA_PENCERE_SN:.0f} sn içinde yeniden açılırsa: "
                              "aradaki kapalı süre × musluk debisi (ör. sabunlama arası). Ara "
                              f"{KURTARMA_PENCERE_SN:.0f} sn'yi geçerse sayılmaz; normal kullanımda kurtarılan su artmaz."],
            ["Musluk debisi (L/dk)", f"ESP32 debi gönderirse ölçülen değer, göndermezse {s(SENSORLU_DEBI_LS * 60, 1)}"],
            ["Pompa özgül enerjisi (kWh/m³)", s(POMPA_KWH_M3, 3)],
            ["Şebeke emisyon faktörü (kg CO2/kWh)", s(SEBEKE_EMISYON, 3)],
            ["Su temin + arıtma karbonu (kg CO2e/m³)", s(SU_TEMIN_KARBON, 3)],
            ["Ağaç eşdeğeri", f"1 olgun ağaç ≈ {s(AGAC_KG_YIL, 1)} kg CO2/yıl; ağaç-gün = önlenen CO2e / ({s(AGAC_KG_YIL, 1)}/365)"],
            ["Not", "Katsayılar varsayılandır; resmi raporlamada kurumun ölçülmüş değerleriyle güncellenmelidir."],
        ]:
            w.writerow(satir)
        return "﻿" + out.getvalue()

    # ── kalıcılık ──
    def kaydet(self) -> None:
        with self.kilit:
            veri = {
                "surum": 1,
                "aktif_tarih": self.aktif_tarih.isoformat(),
                "gun_no": self.gun_no,
                "sim_aktif": self.sim_aktif,
                "ariza_sayac": self.ariza_sayac,
                "gunluk": {b: {str(k): v for k, v in katlar.items()} for b, katlar in self.gunluk.items()},
                "kilitler": {b: {str(k): {"kilitli": kat["kilitli"], "aktif_ariza": kat["aktif_ariza"]}
                                 for k, kat in katlar.items()} for b, katlar in self.katlar.items()},
                "gecmis": self.gecmis,
                "arizalar": self.arizalar,
            }
            try:
                os.makedirs(os.path.dirname(VERI_DOSYASI), exist_ok=True)
                gecici = VERI_DOSYASI + ".tmp"
                with open(gecici, "w", encoding="utf-8") as f:
                    json.dump(veri, f, ensure_ascii=False)
                os.replace(gecici, VERI_DOSYASI)
                self._son_kayit = time.time()
            except OSError as hata:
                log.error("Durum dosyası yazılamadı: %s", hata)

    def _yukle(self) -> None:
        if not os.path.exists(VERI_DOSYASI):
            return
        try:
            with open(VERI_DOSYASI, encoding="utf-8") as f:
                v = json.load(f)
            aktif_tarih = date.fromisoformat(v["aktif_tarih"])
            gunluk = self._bos_gunluk()
            for b in BINALAR:
                for k in KATLAR:
                    kayit = v.get("gunluk", {}).get(b, {}).get(str(k)) or {}
                    for anahtar in gunluk[b][k]:
                        if anahtar in kayit:
                            gunluk[b][k][anahtar] = type(gunluk[b][k][anahtar])(kayit[anahtar])
            self.aktif_tarih, self.gunluk = aktif_tarih, gunluk
            self.gun_no = int(v.get("gun_no", 1))
            self.sim_aktif = bool(v.get("sim_aktif", True))
            self.gecmis = list(v.get("gecmis", []))
            self.arizalar = list(v.get("arizalar", []))
            self.ariza_sayac = int(v.get("ariza_sayac", len(self.arizalar)))
            for b in BINALAR:
                for k in KATLAR:
                    kl = v.get("kilitler", {}).get(b, {}).get(str(k)) or {}
                    self.katlar[b][k].update(kilitli=bool(kl.get("kilitli")), aktif_ariza=kl.get("aktif_ariza"))
            log.info("Kayıt yüklendi: %s (Gün %s), %s arşiv günü, %s arıza.",
                     self.aktif_tarih, self.gun_no, len(self.gecmis), len(self.arizalar))
        except (OSError, ValueError, KeyError, TypeError) as hata:
            log.warning("Kayıt dosyası okunamadı (%s); temiz başlangıç yapılıyor.", hata)


# ══════════════════════════════ FLASK ══════════════════════════════
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
app.json.ensure_ascii = False
app.json.compact = True
app.json.sort_keys = False

platform = Platform()
atexit.register(platform.kaydet)

_motor_kilit = threading.Lock()
_motor_calisiyor = False


def motoru_baslat() -> None:
    """Sensör motorunu (tek seferlik) arka planda başlatır."""
    global _motor_calisiyor
    with _motor_kilit:
        if _motor_calisiyor:
            return
        _motor_calisiyor = True
    threading.Thread(target=_motor_dongusu, name="sensor-motoru", daemon=True).start()


def _motor_dongusu() -> None:
    onceki = time.monotonic()
    while True:
        time.sleep(TICK_SN)
        simdi = time.monotonic()
        dt, onceki = min(simdi - onceki, 2.0), simdi
        try:
            platform.tick(dt)
        except Exception:
            log.exception("Motor adımında hata")


@app.before_request
def _motor_kontrol():
    motoru_baslat()


def _admin_mi() -> bool:
    return session.get("admin") is True


def _veri() -> dict:
    return request.get_json(silent=True) or request.form.to_dict() or {}


def _konum(v: dict):
    bina = str(v.get("bina", "")).strip().upper().translate(TR_ASCII)
    try:
        kat = int(v.get("kat"))
    except (TypeError, ValueError):
        return None
    return (bina, kat) if bina in BINALAR and kat in KATLAR else None


def _bit(deger) -> int:
    if isinstance(deger, str):
        return 1 if deger.strip().lower() in ("1", "true", "on", "evet") else 0
    return 1 if deger else 0


def _hata(mesaj: str, kod: int = 400):
    return jsonify({"ok": False, "hata": mesaj}), kod


@app.get("/")
def anasayfa():
    return render_template("index.html", kurum=KURUM_ADI)


@app.get("/api/durum")
def api_durum():
    return jsonify(platform.durum(admin=_admin_mi()))


@app.get("/api/gecmis")
def api_gecmis():
    return jsonify({"gecmis": platform.gecmis_al()})


@app.post("/api/sensor")
def api_sensor():
    """ESP32 düğümlerinin veri gönderdiği uç nokta. Yanıttaki `kilitli` alanı valfi sürer."""
    v = _veri()
    konum = _konum(v)
    if not konum:
        return _hata("Geçersiz bina/kat. Bina: YAZILIM, EHM, INSAAT · Kat: 1-3")
    kaynak = str(v.get("kaynak", "ESP32")).upper()
    if kaynak not in ("ESP32", "PANEL"):
        kaynak = "ESP32"
    debi = None
    if v.get("debi_lpm") not in (None, ""):
        try:
            debi = float(v["debi_lpm"])
        except (TypeError, ValueError):
            return _hata("debi_lpm sayısal olmalı (L/dk).")
        if not 0 < debi < 60:
            debi = None
    durum = platform.sensor_guncelle(*konum, _bit(v.get("el")), _bit(v.get("akis")), kaynak, debi)
    return jsonify({"ok": True, **durum, "tarih": platform.aktif_tarih.isoformat()})


@app.get("/api/kat/<bina>/<int:kat>")
def api_kat(bina: str, kat: int):
    """Firmware'in yalnızca kilit durumunu sorgulaması için hafif uç nokta."""
    konum = _konum({"bina": bina, "kat": kat})
    if not konum:
        return _hata("Geçersiz bina/kat.", 404)
    with platform.kilit:
        return jsonify({"ok": True, **platform._kat_durum(*konum)})


@app.post("/api/sim/otomatik")
def api_sim_otomatik():
    platform.sim_ayarla(_bit(_veri().get("aktif")) == 1)
    return jsonify({"ok": True, "sim_aktif": platform.sim_aktif})


@app.post("/api/sim/kacak")
def api_sim_kacak():
    konum = _konum(_veri())
    if not konum:
        return _hata("Geçersiz bina/kat.")
    return jsonify({"ok": True, **platform.kacak_senaryosu(*konum)})


@app.post("/api/sim/birak")
def api_sim_birak():
    konum = _konum(_veri())
    if not konum:
        return _hata("Geçersiz bina/kat.")
    return jsonify({"ok": True, **platform.panel_birak(*konum)})


@app.post("/api/gun/bitir")
def api_gun_bitir():
    ozet = platform.gunu_bitir()
    return jsonify({"ok": True, "arsivlenen": ozet["etiket"],
                    "yeni_tarih": tarih_etiketi(platform.aktif_tarih), "gun_no": platform.gun_no})


@app.post("/api/admin/giris")
def api_admin_giris():
    sifre = str(_veri().get("sifre", ""))
    if hmac.compare_digest(sifre.encode("utf-8"), ADMIN_SIFRE.encode("utf-8")):
        session["admin"] = True
        return jsonify({"ok": True})
    time.sleep(0.6)  # kaba kuvvet denemelerini yavaşlat
    return _hata("Şifre hatalı.", 401)


@app.post("/api/admin/cikis")
def api_admin_cikis():
    session.pop("admin", None)
    return jsonify({"ok": True})


@app.post("/api/ariza/<int:ariza_id>/coz")
def api_ariza_coz(ariza_id: int):
    if not _admin_mi():
        return _hata("Bu işlem için yönetici yetkisi gerekli.", 403)
    ariza, hata = platform.ariza_coz(ariza_id)
    if hata:
        return _hata(hata, 409)
    return jsonify({"ok": True, "ariza": ariza})


@app.get("/rapor/excel")
def rapor_excel():
    icerik = platform.csv_rapor()
    dosya = f"Kampus_Su_Enerji_Raporu_{platform.aktif_tarih.isoformat()}.csv"
    return Response(icerik.encode("utf-8"), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{dosya}"',
                             "Cache-Control": "no-store"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    motoru_baslat()
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")),
            debug=False, threaded=True)
