import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface MarketStatus {
  is_open: boolean;
  is_holiday: boolean;
  holiday_name?: string;
  is_weekend: boolean;
  time_ist: string;
  next_session: string;
}

export interface OverviewData {
  market: MarketStatus;
  run_mode: string;
  execution_mode: string;
  dry_run: boolean;
  portfolio: {
    initial_capital: number;
    open_positions_count: number;
    capital_deployed: number;
    realized_pnl: number;
    total_trades_count: number;
  };
  regime: {
    status: string;
    exposure_multiplier: number;
    benchmark_symbol: string;
    benchmark_close: number;
    above_200_dma: boolean;
    breadth_pct: number;
    breadth_above_50_dma: boolean;
  };
  database: {
    instruments: number;
    candles: number;
    signals: number;
    trades: number;
    orders: number;
    events: number;
    equity_records: number;
    tasks: number;
  };
  active_tasks?: Array<{
    task_id: string;
    task_type: string;
    status: string;
    progress_pct: number;
    current_step: string;
    completed_steps: number;
    total_steps: number;
    stage?: string;
    stage_num?: number;
    total_stages?: number;
    stage_progress_pct?: number;
    current_item?: string;
    message: string;
  }>;
}


@Injectable({
  providedIn: 'root',
})
export class ApiService {
  private http = inject(HttpClient);
  private baseUrl = typeof window !== 'undefined' && window.location.port === '8000' 
    ? '/api' 
    : 'http://127.0.0.1:8000/api';

  // System & Overview
  getHealth(): Observable<any> {
    return this.http.get(`${this.baseUrl}/health`);
  }

  getOverview(): Observable<OverviewData> {
    return this.http.get<OverviewData>(`${this.baseUrl}/overview/`);
  }

  // Universe
  getUniverse(search: string = '', page: number = 1, pageSize: number = 50): Observable<any> {
    return this.http.get(`${this.baseUrl}/universe/`, {
      params: { search, page: page.toString(), page_size: pageSize.toString() }
    });
  }

  buildUniverse(mode: string = 'full', maxTickers: number = 250): Observable<any> {
    return this.http.post(`${this.baseUrl}/universe/build`, { mode, max_tickers: maxTickers });
  }

  preflightUniverse(): Observable<any> {
    return this.http.get(`${this.baseUrl}/universe/preflight`);
  }

  // Data
  getDataStats(): Observable<any> {
    return this.http.get(`${this.baseUrl}/data/stats`);
  }

  getCandles(symbol: string, limit: number = 250): Observable<any> {
    return this.http.get(`${this.baseUrl}/data/candles/${symbol}`, {
      params: { limit: limit.toString() }
    });
  }

  backfillData(symbols: string[] = [], lookbackBars: number = 400): Observable<any> {
    return this.http.post(`${this.baseUrl}/data/backfill`, {
      symbols,
      lookback_bars: lookbackBars
    });
  }

  // Screener
  getLatestScreener(): Observable<any> {
    return this.http.get(`${this.baseUrl}/screener/latest`);
  }

  runScreener(saveSignals: boolean = true): Observable<any> {
    return this.http.post(`${this.baseUrl}/screener/run`, { save_signals: saveSignals });
  }

  getCandidateDetails(symbol: string): Observable<any> {
    return this.http.get(`${this.baseUrl}/screener/candidate/${symbol}`);
  }

  // Trades & Execution
  getTrades(state?: string): Observable<any> {
    return this.http.get(`${this.baseUrl}/trades/`, {
      params: state ? { state } : undefined
    });
  }


  getTradeHistory(limit: number = 100): Observable<any> {
    return this.http.get(`${this.baseUrl}/trades/history`, {
      params: { limit: limit.toString() }
    });
  }

  getOrders(limit: number = 100): Observable<any> {
    return this.http.get(`${this.baseUrl}/trades/orders`, {
      params: { limit: limit.toString() }
    });
  }

  approveSignal(signalId: number): Observable<any> {
    return this.http.post(`${this.baseUrl}/trades/approve/${signalId}`, {});
  }

  rejectSignal(signalId: number, reason: string = 'User rejected'): Observable<any> {
    return this.http.post(`${this.baseUrl}/trades/reject/${signalId}`, { reason });
  }

  closeTrade(tradeId: number, reason: string = 'User manual exit'): Observable<any> {
    return this.http.post(`${this.baseUrl}/trades/close/${tradeId}`, { reason });
  }

  // Backtest
  runBacktest(params: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/backtest/run`, params);
  }

  runWalkForward(params: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/backtest/walk-forward`, params);
  }

  // Settings
  getSettings(): Observable<any> {
    return this.http.get(`${this.baseUrl}/settings/`);
  }

  updateSettings(settings: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/settings/`, settings);
  }

  resetSettings(): Observable<any> {
    return this.http.post(`${this.baseUrl}/settings/reset`, {});
  }

  testGroww(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/settings/test-groww`, data);
  }

  testGemini(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/settings/test-gemini`, data);
  }

  testTelegram(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/settings/test-telegram`, data);
  }

  // Tasks & Resumability
  getTasks(): Observable<any> {
    return this.http.get(`${this.baseUrl}/tasks/`);
  }

  getTask(taskId: string): Observable<any> {
    return this.http.get(`${this.baseUrl}/tasks/${taskId}`);
  }

  pauseTask(taskId: string): Observable<any> {
    return this.http.post(`${this.baseUrl}/tasks/${taskId}/pause`, {});
  }

  resumeTask(taskId: string): Observable<any> {
    return this.http.post(`${this.baseUrl}/tasks/${taskId}/resume`, {});
  }

  cancelTask(taskId: string): Observable<any> {
    return this.http.post(`${this.baseUrl}/tasks/${taskId}/cancel`, {});
  }

  clearTasks(status: string = 'finished'): Observable<any> {
    return this.http.delete(`${this.baseUrl}/tasks/clear`, { params: { status } });
  }

  deleteTask(taskId: string): Observable<any> {
    return this.http.delete(`${this.baseUrl}/tasks/${taskId}`);
  }

  getLogStreamUrl(): string {
    return `${this.baseUrl}/tasks/logs/stream`;
  }

  getRecentLogs(limit: number = 150): Observable<{ logs: any[]; count: number }> {
    return this.http.get<{ logs: any[]; count: number }>(`${this.baseUrl}/tasks/logs/recent`, {
      params: { limit: limit.toString() }
    });
  }
}
