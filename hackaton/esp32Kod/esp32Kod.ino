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
String serverName = "http://172.20.10.5:5000/api/veri";

// Pin Tanımlamaları
// 1. Sensör (Eski Işık Sensörü) -> Artık SU AKIŞ / KAÇAK SENSÖRÜ
const int trigAkis = 12; 
const int echoAkis = 14;

// 2. Sensör -> MUSLUK EL SENSÖRÜ
const int trigMusluk = 27; 
const int echoMusluk = 26;

const int btnBina = 32; 
const int btnKat = 33;

String binalar[3] = {"YAZILIM", "EHM", "INSAAT"};
int binaIndex = 0;
int kat = 1;
unsigned long sonWifiZamani = 0;

// Sunucudan gelen arıza kilidi durumu
bool arizaKilitli = false;

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

void setup() {
  pinMode(trigAkis, OUTPUT); pinMode(echoAkis, INPUT);
  pinMode(trigMusluk, OUTPUT); pinMode(echoMusluk, INPUT);
  pinMode(btnBina, INPUT_PULLUP); pinMode(btnKat, INPUT_PULLUP);

  if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    for(;;);
  }
  
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 10);
  display.print("WiFi Baglaniliyor...");
  display.setCursor(0, 30);
  display.print(ssid);
  display.display();

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }
  
  display.clearDisplay();
  display.setCursor(0, 20);
  display.print("Baglandi!");
  display.display();
  delay(1000);
}

void loop() {
  // Bina Değiştirme Düğmesi
  bool suankiBinaDurum = digitalRead(btnBina);
  if (suankiBinaDurum == LOW && sonBinaDurum == HIGH && (millis() - sonBinaZaman > debounceSuresi)) {
    binaIndex = (binaIndex + 1) % 3;
    sonBinaZaman = millis();
  }
  sonBinaDurum = suankiBinaDurum;
  
  // Kat Değiştirme Düğmesi
  bool suankiKatDurum = digitalRead(btnKat);
  if (suankiKatDurum == LOW && sonKatDurum == HIGH && (millis() - sonKatZaman > debounceSuresi)) {
    kat = (kat == 3) ? 1 : kat + 1;
    sonKatZaman = millis();
  }
  sonKatDurum = suankiKatDurum;

  // Sensör Okumaları
  int akisMesafe = mesafeOlc(trigAkis, echoAkis);
  int muslukMesafe = mesafeOlc(trigMusluk, echoMusluk);

  // Akış sensörü (15 cm altı su akıyor demektir)
  bool suAkisiVar = (akisMesafe > 0 && akisMesafe < 15);
  // Musluk el sensörü (15 cm altı kullanıcı elini uzattı demektir)
  bool elUzandi = (muslukMesafe > 0 && muslukMesafe < 15);

  // OLED Ekran Çizimi
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(0, 0); 
  display.print(binalar[binaIndex]);
  
  display.setCursor(0, 18); 
  display.print("KAT: "); 
  display.print(kat);
  
  display.setTextSize(1);
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

  // Saniyede 1 Sunucuya Gönder ve Kilit Durumunu Al
  if (WiFi.status() == WL_CONNECTED && (millis() - sonWifiZamani >= 1000)) {
    HTTPClient http;
    http.begin(serverName);
    http.setTimeout(400);
    http.addHeader("Content-Type", "application/json");
    
    String jsonVeri = "{\"bina\":\"" + binalar[binaIndex] + "\", \"kat\":" + String(kat) + ", \"akis\":" + (suAkisiVar ? "1" : "0") + ", \"el\":" + (elUzandi ? "1" : "0") + "}";
    int httpCode = http.POST(jsonVeri);
    
    if (httpCode > 0) {
      String yanit = http.getString();
      // Sunucudan gelen yanıtta kilitli:true varsa musluğu kilitle
      if (yanit.indexOf("\"kilitli\":true") != -1 || yanit.indexOf("\"kilitli\": true") != -1) {
        arizaKilitli = true;
      } else {
        arizaKilitli = false;
      }
    }
    http.end();
    sonWifiZamani = millis();
  }
}