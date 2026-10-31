#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <HTTPClient.h>

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// --- AĞ BİLGİLERİN ---
const char* ssid = "Toprak";
const char* password = "eymen3455";
// DÜZELTME 1: Flask'taki uç nokta /api/sensor (eski /api/veri sunucuda yok -> 404)
String serverName = "http://172.20.10.5:5000/api/sensor";

// Pin Tanımlamaları
// 1. Sensör -> SU AKIŞ / KAÇAK SENSÖRÜ (HC-SR04)
const int trigAkis = 12;
const int echoAkis = 14;

// 2. Sensör -> MUSLUK EL SENSÖRÜ (HC-SR04)
const int trigMusluk = 27;
const int echoMusluk = 26;

const int btnBina = 32;
const int btnKat = 33;

// İsteğe bağlı: kat solenoid valfi için röle. Röle bağladıysan true yap.
// HIGH = valf açık, LOW = valf kapalı (kat kilitli -> musluk su vermez)
const bool VALF_VAR = false;
const int pinValf = 25;

String binalar[3] = {"YAZILIM", "EHM", "INSAAT"};
int binaIndex = 0;
int kat = 1;
unsigned long sonWifiZamani = 0;
unsigned long sonBaglantiDenemesi = 0;

// Sunucudan gelen arıza kilidi durumu
bool arizaKilitli = false;
int sonHttpKodu = 0;   // OLED'de sunucu durumunu göstermek için

// Düğme Debounce Değişkenleri
bool sonBinaDurum = HIGH;
bool sonKatDurum = HIGH;
unsigned long sonBinaZaman = 0;
unsigned long sonKatZaman = 0;
const int debounceSuresi = 200;

int mesafeOlc(int trig, int echo) {
  digitalWrite(trig, LOW); delayMicroseconds(2);
  digitalWrite(trig, HIGH); delayMicroseconds(10);
  digitalWrite(trig, LOW);
  long duration = pulseIn(echo, HIGH, 30000);
  if (duration == 0) return 0;
  return duration * 0.034 / 2;
}

// Bir kat için ölçümü sunucuya gönderir. kilitOku=true ise yanıttan kilit durumunu okur.
int veriGonder(int bIndex, int k, bool el, bool akis, bool kilitOku) {
  if (WiFi.status() != WL_CONNECTED) return -1;
  HTTPClient http;
  http.begin(serverName);
  http.setTimeout(800);
  http.addHeader("Content-Type", "application/json");

  String jsonVeri = "{\"bina\":\"" + binalar[bIndex] + "\",\"kat\":" + String(k) +
                    ",\"el\":" + (el ? "1" : "0") +
                    ",\"akis\":" + (akis ? "1" : "0") +
                    ",\"kaynak\":\"ESP32\"}";
  int httpCode = http.POST(jsonVeri);

  // DÜZELTME 2: Sadece 200 yanıtında kilidi güncelle.
  // (Eskiden 404 gibi hata yanıtları da kilidi sessizce "false" yapıyordu.)
  if (httpCode == 200 && kilitOku) {
    String yanit = http.getString();
    arizaKilitli = (yanit.indexOf("\"kilitli\":true") != -1 ||
                    yanit.indexOf("\"kilitli\": true") != -1);
  }
  http.end();
  return httpCode;
}

// DÜZELTME 3: Bina/kat değişince eski kata "boşta" (el=0, akis=0) gönder.
// Yoksa sunucu eski katın son durumunu 15 sn daha geçerli sayar:
// hayalet tüketim yazar veya akış varken geçildiyse eski katı yanlışlıkla kaçaktan kilitler.
void konumDegisti(int eskiBina, int eskiKat) {
  veriGonder(eskiBina, eskiKat, false, false, false);
  arizaKilitli = false;   // yeni katın kilidi ilk yanıtta öğrenilir
  sonWifiZamani = 0;      // yeni kata hemen gönder
}

void setup() {
  pinMode(trigAkis, OUTPUT); pinMode(echoAkis, INPUT);
  pinMode(trigMusluk, OUTPUT); pinMode(echoMusluk, INPUT);
  pinMode(btnBina, INPUT_PULLUP); pinMode(btnKat, INPUT_PULLUP);
  if (VALF_VAR) { pinMode(pinValf, OUTPUT); digitalWrite(pinValf, HIGH); }

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    for (;;);
  }

  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 10);
  display.print("WiFi Baglaniliyor...");
  display.setCursor(0, 30);
  display.print(ssid);
  display.display();

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }

  display.clearDisplay();
  display.setCursor(0, 20);
  display.print("Baglandi!");
  display.setCursor(0, 34);
  display.print(WiFi.localIP());
  display.display();
  delay(1000);
}

void loop() {
  // Bina Değiştirme Düğmesi
  bool suankiBinaDurum = digitalRead(btnBina);
  if (suankiBinaDurum == LOW && sonBinaDurum == HIGH && (millis() - sonBinaZaman > debounceSuresi)) {
    int eskiBina = binaIndex;
    binaIndex = (binaIndex + 1) % 3;
    sonBinaZaman = millis();
    konumDegisti(eskiBina, kat);
  }
  sonBinaDurum = suankiBinaDurum;

  // Kat Değiştirme Düğmesi
  bool suankiKatDurum = digitalRead(btnKat);
  if (suankiKatDurum == LOW && sonKatDurum == HIGH && (millis() - sonKatZaman > debounceSuresi)) {
    int eskiKat = kat;
    kat = (kat == 3) ? 1 : kat + 1;
    sonKatZaman = millis();
    konumDegisti(binaIndex, eskiKat);
  }
  sonKatDurum = suankiKatDurum;

  // Sensör Okumaları (iki HC-SR04 birbirinin yankısını yakalamasın diye kısa ara)
  int akisMesafe = mesafeOlc(trigAkis, echoAkis);
  delay(10);
  int muslukMesafe = mesafeOlc(trigMusluk, echoMusluk);

  // Akış sensörü (15 cm altı su akıyor demektir)
  bool suAkisiVar = (akisMesafe > 0 && akisMesafe < 15);
  // Musluk el sensörü (15 cm altı kullanıcı elini uzattı demektir)
  bool elUzandi = (muslukMesafe > 0 && muslukMesafe < 15);

  // WiFi koparsa 5 sn'de bir yeniden bağlan (loop'u kilitlemeden)
  if (WiFi.status() != WL_CONNECTED && millis() - sonBaglantiDenemesi > 5000) {
    WiFi.reconnect();
    sonBaglantiDenemesi = millis();
  }

  // Saniyede 1 Sunucuya Gönder ve Kilit Durumunu Al
  if (WiFi.status() == WL_CONNECTED && (millis() - sonWifiZamani >= 1000)) {
    sonHttpKodu = veriGonder(binaIndex, kat, elUzandi, suAkisiVar, true);
    sonWifiZamani = millis();
  }

  // Kilitliyse valfi kapat (röle bağlıysa)
  if (VALF_VAR) digitalWrite(pinValf, arizaKilitli ? LOW : HIGH);

  // OLED Ekran Çizimi
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.print(binalar[binaIndex]);

  display.setCursor(0, 18);
  display.print("KAT: ");
  display.print(kat);

  // Sunucu durumu: SRV OK / E404 / SRV -- / WiFi X
  display.setTextSize(1);
  display.setCursor(86, 22);
  if (WiFi.status() != WL_CONNECTED) display.print("WiFi X");
  else if (sonHttpKodu == 200) display.print("SRV OK");
  else if (sonHttpKodu <= 0) display.print("SRV --");
  else { display.print("E"); display.print(sonHttpKodu); }

  if (arizaKilitli) {
    display.setCursor(0, 42);
    display.print("!! ARIZA KILITLI !!");
    display.setCursor(0, 54);
    display.print("ADMIN ONAYI BEKLIYOR");
  } else {
    display.setCursor(0, 42);
    display.print(elUzandi ? "MUSLUK: ACIK (EL VAR)" : "MUSLUK: KAPALI");
    display.setCursor(0, 54);
    display.print(suAkisiVar ? "AKIS: ALGILANDI (~)" : "AKIS: YOK");
  }
  display.display();
}
