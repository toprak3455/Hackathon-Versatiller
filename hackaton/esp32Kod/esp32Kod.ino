#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <HTTPClient.h>

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// --- BURAYI KENDİ BİLGİLERİNLE DOLDUR ---
const char* ssid = "Toprak";           // Evdeki Wi-Fi adın
const char* password = "eymen3455";     // Evdeki Wi-Fi şifren
String serverName = "http://172.20.10.5:5000/api/veri";
// Pin Tanımlamaları
const int trigKapi = 12; 
const int echoKapi = 14;
const int trigLavabo = 27; 
const int echoLavabo = 26;
const int btnBina = 32; 
const int btnKat = 33;

// Durum Değişkenleri
String binalar[3] = {"YAZILIM", "EHM", "INSAAT"};
int binaIndex = 0;
int kat = 1;
unsigned long sonWifiZamani = 0;

// Düğme Kronometreleri
bool sonBinaDurum = HIGH;
bool sonKatDurum = HIGH;
unsigned long sonBinaZaman = 0;
unsigned long sonKatZaman = 0;
const int debounceSuresi = 200; // 200ms güvenli tıklama koruması

int mesafeOlc(int trig, int echo) {
  digitalWrite(trig, LOW); delayMicroseconds(2);
  digitalWrite(trig, HIGH); delayMicroseconds(10);
  digitalWrite(trig, LOW);
  long duration = pulseIn(echo, HIGH, 30000); 
  if (duration == 0) return 0; 
  return duration * 0.034 / 2;
}

void setup() {
  pinMode(trigKapi, OUTPUT); pinMode(echoKapi, INPUT);
  pinMode(trigLavabo, OUTPUT); pinMode(echoLavabo, INPUT);
  pinMode(btnBina, INPUT_PULLUP); pinMode(btnKat, INPUT_PULLUP);

  if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    for(;;);
  }
  
  // OLED'de Wi-Fi Bekleme Ekranı
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 10);
  display.print("WiFi Baglaniliyor...");
  display.setCursor(0, 30);
  display.print(ssid);
  display.display();

  // Kendi Ev Ağına Bağlanma
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }
  
  display.clearDisplay();
  display.setCursor(0, 20);
  display.print("Baglandi!");
  display.display();
  delay(1500);
}

void loop() {
  // 1. DÜĞME (Kilitlenmesiz Anlık Okuma)
  bool suankiBinaDurum = digitalRead(btnBina);
  if (suankiBinaDurum == LOW && sonBinaDurum == HIGH && (millis() - sonBinaZaman > debounceSuresi)) {
    binaIndex = (binaIndex + 1) % 3;
    sonBinaZaman = millis();
  }
  sonBinaDurum = suankiBinaDurum;
  
  // 2. DÜĞME (Kilitlenmesiz Anlık Okuma)
  bool suankiKatDurum = digitalRead(btnKat);
  if (suankiKatDurum == LOW && sonKatDurum == HIGH && (millis() - sonKatZaman > debounceSuresi)) {
    kat = (kat == 3) ? 1 : kat + 1;
    sonKatZaman = millis();
  }
  sonKatDurum = suankiKatDurum;

  int kapiMesafe = mesafeOlc(trigKapi, echoKapi);
  int lavaboMesafe = mesafeOlc(trigLavabo, echoLavabo);

  bool isikAcik = (kapiMesafe > 0 && kapiMesafe < 50);
  bool suAkiyor = (lavaboMesafe > 0 && lavaboMesafe < 15);

  // OLED Ekran Anlık Çizimi
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(0, 0); 
  display.print(binalar[binaIndex]);
  
  display.setCursor(0, 20); 
  display.print("KAT: "); 
  display.print(kat);
  
  display.setTextSize(1);
  display.setCursor(0, 45); 
  display.print(isikAcik ? "ISIK: ACIK (*)" : "ISIK: KAPALI");
  
  display.setCursor(0, 55); 
  display.print(suAkiyor ? "SU: AKIYOR (~)" : "SU: KAPALI");
  display.display();

  // Web Sunucusuna Arka Planda Hızlı Veri Gönderme
  if (WiFi.status() == WL_CONNECTED && (millis() - sonWifiZamani >= 1000)) {
    HTTPClient http;
    http.begin(serverName);
    http.setTimeout(300); // Sistemin donmasını engelleyen kritik hamle!
    http.addHeader("Content-Type", "application/json");
    
    String jsonVeri = "{\"bina\":\"" + binalar[binaIndex] + "\", \"kat\":" + String(kat) + ", \"isik\":" + (isikAcik ? "1" : "0") + ", \"su\":" + (suAkiyor ? "1" : "0") + "}";
    http.POST(jsonVeri);
    http.end();
    sonWifiZamani = millis();
  }
}