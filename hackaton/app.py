from flask import Flask, request, jsonify, render_template
import copy

app = Flask(__name__)

binalar = ["YAZILIM", "EHM", "INSAAT"]
tuketim = {b: {k: {"su_harcanan_sn": 0, "su_kurtarilan_sn": 0, "elektrik_sn": 0} for k in [1, 2, 3]} for b in binalar}
sensor_durum = {b: {k: {"su_onceki": 0, "su_kapali_sn": 0} for k in [1, 2, 3]} for b in binalar}
aktif_durum = {"bina": "YAZILIM", "kat": 1, "isik": 0, "su": 0}

aktif_gun = 1
gecmis_gunler = {} 

kesintisiz_su_suresi = 0
sizinti_alarmi = False
sizinti_konum = ""

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/veri', methods=['POST'])
def veri_al():
    global aktif_durum, kesintisiz_su_suresi, sizinti_alarmi, sizinti_konum
    veri = request.json
    
    bina = veri.get('bina')
    kat = int(veri.get('kat'))
    isik = int(veri.get('isik'))
    su = int(veri.get('su'))

    aktif_durum = {"bina": bina, "kat": kat, "isik": isik, "su": su}

    if isik == 1: 
        tuketim[bina][kat]["elektrik_sn"] += 1
    
    if su == 1: 
        kesintisiz_su_suresi += 1
        if kesintisiz_su_suresi >= 10: 
            sizinti_alarmi = True
            sizinti_konum = f"{bina} Fakültesi {kat}. Kat"
        else:
            tuketim[bina][kat]["su_harcanan_sn"] += 1
    else:
        kesintisiz_su_suresi = 0
        sizinti_alarmi = False
        sizinti_konum = ""

    onceki_su = sensor_durum[bina][kat]["su_onceki"]

    if su == 0:
        sensor_durum[bina][kat]["su_kapali_sn"] += 1
    elif su == 1 and onceki_su == 0:
        kapali_sure = sensor_durum[bina][kat]["su_kapali_sn"]
        if 0 < kapali_sure <= 7: 
            tuketim[bina][kat]["su_kurtarilan_sn"] += kapali_sure
        
        sensor_durum[bina][kat]["su_kapali_sn"] = 0

    sensor_durum[bina][kat]["su_onceki"] = su

    return jsonify({"status": "success"})

@app.route('/api/durum', methods=['GET'])
def durum_ver():
    return jsonify({
        "aktif": aktif_durum,
        "tuketim": tuketim,
        "gun": aktif_gun,
        "gecmis": gecmis_gunler,
        "alarm": sizinti_alarmi,
        "alarm_konum": sizinti_konum
    })

@app.route('/api/gun_atla', methods=['POST'])
def gun_atla():
    global tuketim, aktif_gun, gecmis_gunler, sensor_durum
    
    en_dusuk = 999999
    en_tasarruflu = ""
    for b in binalar:
        toplam = sum([tuketim[b][k]["su_harcanan_sn"] + tuketim[b][k]["elektrik_sn"] for k in [1,2,3]])
        if toplam < en_dusuk:
            en_dusuk = toplam
            en_tasarruflu = b

    gecmis_gunler[str(aktif_gun)] = {
        "tuketim": copy.deepcopy(tuketim),
        "en_tasarruflu": en_tasarruflu
    }

    aktif_gun += 1
    
    # HATA BURADAYDI: Sadece sayaçları değil, algoritmik zaman hafızasını da tamamen SIFIRLIYORUZ!
    for b in binalar:
        for k in [1, 2, 3]:
            tuketim[b][k]["su_harcanan_sn"] = 0
            tuketim[b][k]["su_kurtarilan_sn"] = 0
            tuketim[b][k]["elektrik_sn"] = 0
            sensor_durum[b][k]["su_onceki"] = 0
            sensor_durum[b][k]["su_kapali_sn"] = 0

    return jsonify({"status": "success", "yeni_gun": aktif_gun})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)