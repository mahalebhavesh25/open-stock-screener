import { Component, OnInit, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api.service';

@Component({
  selector: 'app-trades',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './trades.component.html',
  styleUrls: ['./trades.component.css']
})
export class TradesComponent implements OnInit {
  private api = inject(ApiService);

  activeTab = signal<'OPEN' | 'PROPOSED' | 'ORDERS' | 'HISTORY'>('OPEN');
  openTrades = signal<any[]>([]);
  proposedTrades = signal<any[]>([]);
  orders = signal<any[]>([]);
  tradeHistory = signal<any[]>([]);
  loading = signal<boolean>(false);
  message = signal<string>('');

  totalRealizedPnL = computed(() => {
    return this.tradeHistory().reduce((sum, t) => sum + (t.realized_pnl || 0), 0);
  });

  winCount = computed(() => {
    return this.tradeHistory().filter(t => (t.realized_pnl || 0) > 0).length;
  });

  lossCount = computed(() => {
    return this.tradeHistory().filter(t => (t.realized_pnl || 0) < 0).length;
  });

  winRate = computed(() => {
    const total = this.winCount() + this.lossCount();
    return total > 0 ? (this.winCount() / total) * 100 : 0;
  });

  ngOnInit(): void {
    this.refreshAll();
  }

  refreshAll(): void {
    this.loading.set(true);

    this.api.getTrades('OPEN').subscribe({
      next: (res) => this.openTrades.set(res.trades || []),
      error: (err) => console.error('Open trades load failed', err)
    });

    this.api.getTrades('PROPOSED').subscribe({
      next: (res) => this.proposedTrades.set(res.trades || []),
      error: (err) => console.error('Proposed trades load failed', err)
    });

    this.api.getOrders().subscribe({
      next: (res) => this.orders.set(res.orders || []),
      error: (err) => console.error('Orders load failed', err)
    });

    this.api.getTradeHistory().subscribe({
      next: (res) => {
        this.tradeHistory.set(res.history || []);
        this.loading.set(false);
      },
      error: (err) => {
        console.error('Trade history load failed', err);
        this.loading.set(false);
      }
    });
  }

  approve(trade: any): void {
    if (!trade.signal_id) return;
    this.message.set(`Approving ${trade.symbol}...`);
    this.api.approveSignal(trade.signal_id).subscribe({
      next: () => {
        this.message.set(`Signal approved for ${trade.symbol}`);
        this.refreshAll();
      },
      error: (err) => this.message.set(`Approval failed: ${err.error?.detail || err.message}`)
    });
  }

  reject(trade: any): void {
    if (!trade.signal_id) return;
    this.message.set(`Rejecting ${trade.symbol}...`);
    this.api.rejectSignal(trade.signal_id, 'User rejected in execution desk').subscribe({
      next: () => {
        this.message.set(`Signal rejected for ${trade.symbol}`);
        this.refreshAll();
      },
      error: (err) => this.message.set(`Rejection failed: ${err.error?.detail || err.message}`)
    });
  }

  closePosition(trade: any): void {
    if (!trade.trade_id) return;
    if (!confirm(`Are you sure you want to close position in ${trade.symbol}?`)) return;

    this.message.set(`Closing trade ${trade.symbol}...`);
    this.api.closeTrade(trade.trade_id, 'Manual user exit').subscribe({
      next: () => {
        this.message.set(`Trade ${trade.symbol} closed.`);
        this.refreshAll();
      },
      error: (err) => this.message.set(`Close failed: ${err.error?.detail || err.message}`)
    });
  }
}
