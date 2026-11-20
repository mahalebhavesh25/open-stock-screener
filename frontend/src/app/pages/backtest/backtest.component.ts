import { Component, inject, signal, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api.service';

@Component({
  selector: 'app-backtest',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './backtest.component.html',
  styleUrls: ['./backtest.component.css']
})
export class BacktestComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private eventSource: EventSource | null = null;

  startDate = '2023-01-01';
  endDate = '2026-03-01';
  initialCapital = 1000000;
  selectedStrategy = 'ALL';
  frictionModel = true;

  running = signal<boolean>(false);
  results = signal<any>(null);
  walkForwardResults = signal<any>(null);
  message = signal<string>('');
  logs = signal<any[]>([]);

  ngOnInit(): void {
    this.connectLogStream();
  }

  ngOnDestroy(): void {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }

  connectLogStream(): void {
    try {
      this.eventSource = new EventSource(this.api.getLogStreamUrl());
      this.eventSource.onmessage = (event) => {
        if (!event.data) return;
        try {
          const parsed = JSON.parse(event.data);
          // Only capture logs relevant to backtest, engine, features, screener, and task progress
          const src = (parsed.source || '').toLowerCase();
          if (src.includes('backtest') || src.includes('screener') || src.includes('features') || src.includes('task.progress')) {
            this.logs.update((current) => [...current.slice(-150), parsed]);
          }
        } catch {
          this.logs.update((current) => [...current.slice(-150), {
            timestamp: new Date().toLocaleTimeString(),
            level: 'INFO',
            source: 'engine',
            message: event.data
          }]);
        }
      };
    } catch (e) {
      console.warn('SSE log stream connection error in BacktestComponent:', e);
    }
  }

  runSingleBacktest(): void {
    this.running.set(true);
    this.message.set('Running closed-bar event-driven backtest...');
    this.walkForwardResults.set(null);

    const params = {
      start_date: this.startDate,
      end_date: this.endDate,
      initial_capital: this.initialCapital,
      strategy: this.selectedStrategy,
      friction_model: this.frictionModel
    };

    this.api.runBacktest(params).subscribe({
      next: (res) => {
        this.results.set(res.results || res);
        this.running.set(false);
        this.message.set('Backtest simulation completed.');
      },
      error: (err) => {
        this.running.set(false);
        this.message.set(`Backtest failed: ${err.error?.detail || err.message}`);
      }
    });
  }

  runWalkForward(): void {
    this.running.set(true);
    this.message.set('Running 6-fold purged walk-forward with 10-bar embargo...');
    this.results.set(null);

    const params = {
      start_date: this.startDate,
      end_date: this.endDate,
      initial_capital: this.initialCapital,
      strategy: this.selectedStrategy,
      n_splits: 6,
      embargo_bars: 10
    };

    this.api.runWalkForward(params).subscribe({
      next: (res) => {
        this.walkForwardResults.set(res.results || res);
        this.running.set(false);
        this.message.set('Walk-forward cross-validation completed.');
      },
      error: (err) => {
        this.running.set(false);
        this.message.set(`Walk-forward failed: ${err.error?.detail || err.message}`);
      }
    });
  }

  getKeys(obj: any): string[] {
    return obj ? Object.keys(obj) : [];
  }
}
