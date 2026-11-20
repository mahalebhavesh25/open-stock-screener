import { Routes } from '@angular/router';
import { DashboardComponent } from './pages/dashboard/dashboard.component';
import { ScreenerComponent } from './pages/screener/screener.component';
import { TradesComponent } from './pages/trades/trades.component';
import { UniverseComponent } from './pages/universe/universe.component';
import { BacktestComponent } from './pages/backtest/backtest.component';
import { SettingsComponent } from './pages/settings/settings.component';
import { TasksComponent } from './pages/tasks/tasks.component';

export const routes: Routes = [
  { path: '', redirectTo: 'dashboard', pathMatch: 'full' },
  { path: 'dashboard', component: DashboardComponent },
  { path: 'screener', component: ScreenerComponent },
  { path: 'trades', component: TradesComponent },
  { path: 'universe', component: UniverseComponent },
  { path: 'backtest', component: BacktestComponent },
  { path: 'settings', component: SettingsComponent },
  { path: 'tasks', component: TasksComponent },
  { path: '**', redirectTo: 'dashboard' }
];
