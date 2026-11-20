import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api.service';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './settings.component.html',
  styleUrls: ['./settings.component.css']
})
export class SettingsComponent implements OnInit {
  private api = inject(ApiService);

  cfg: any = {};
  loading = signal<boolean>(true);
  saving = signal<boolean>(false);
  message = signal<string>('');

  testGrowwStatus = signal<string>('');
  testGeminiStatus = signal<string>('');
  testTelegramStatus = signal<string>('');

  ngOnInit(): void {
    this.loadSettings();
  }

  loadSettings(): void {
    this.loading.set(true);
    this.api.getSettings().subscribe({
      next: (data) => {
        this.cfg = { ...data };
        this.loading.set(false);
      },
      error: (err) => {
        console.error('Failed to load settings:', err);
        this.loading.set(false);
      }
    });
  }

  saveSettings(): void {
    this.saving.set(true);
    this.message.set('Saving configuration to SQLite app_settings...');
    this.api.updateSettings(this.cfg).subscribe({
      next: () => {
        this.saving.set(false);
        this.message.set('Settings persisted and applied to live runtime successfully.');
      },
      error: (err) => {
        this.saving.set(false);
        this.message.set(`Save failed: ${err.error?.detail || err.message}`);
      }
    });
  }

  resetDefaults(): void {
    if (!confirm('Are you sure you want to restore all settings to notebook defaults?')) return;
    this.api.resetSettings().subscribe({
      next: (res) => {
        this.cfg = { ...res.config };
        this.message.set('Settings reset to default values.');
      },
      error: (err) => this.message.set(`Reset failed: ${err.error?.detail || err.message}`)
    });
  }

  testGroww(): void {
    this.testGrowwStatus.set('Testing Groww credentials...');
    this.api.testGroww({
      access_token: this.cfg.groww_access_token,
      groww_access_token: this.cfg.groww_access_token,
      api_key: this.cfg.groww_api_key,
      secret_key: this.cfg.groww_secret_key,
      totp_key: this.cfg.groww_totp_key
    }).subscribe({
      next: (res) => this.testGrowwStatus.set(res.success || res.ok ? 'Groww API Connected Successfully!' : `Error: ${res.message || res.error}`),
      error: (err) => this.testGrowwStatus.set(`Connection failed: ${err.error?.detail || err.message}`)
    });
  }


  testGemini(): void {
    this.testGeminiStatus.set('Testing Gemini API key...');
    this.api.testGemini({
      api_key: this.cfg.gemini_api_key,
      model: this.cfg.gemini_model
    }).subscribe({
      next: (res) => this.testGeminiStatus.set(res.success ? 'Gemini LLM Connected Successfully!' : `Error: ${res.message}`),
      error: (err) => this.testGeminiStatus.set(`Connection failed: ${err.error?.detail || err.message}`)
    });
  }

  testTelegram(): void {
    this.testTelegramStatus.set('Sending test alert to Telegram...');
    this.api.testTelegram({
      bot_token: this.cfg.telegram_bot_token,
      chat_id: this.cfg.telegram_chat_id
    }).subscribe({
      next: (res) => this.testTelegramStatus.set(res.success ? 'Telegram Alert Sent Successfully!' : `Error: ${res.message}`),
      error: (err) => this.testTelegramStatus.set(`Connection failed: ${err.error?.detail || err.message}`)
    });
  }
}
