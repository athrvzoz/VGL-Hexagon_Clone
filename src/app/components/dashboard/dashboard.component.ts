import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AppStateService } from '../../services/app-state.service';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './dashboard.component.html',
  styleUrl: './dashboard.component.css'
})
export class DashboardComponent {
  state = inject(AppStateService);

  getStepIconClass(stepName: string): string {
    const name = stepName?.toLowerCase() ?? '';
    if (name.includes('transmittal')) return 'step-icon--orange';
    if (name.includes('submittal')) return 'step-icon--blue';
    if (name.includes('review')) return 'step-icon--green';
    return 'step-icon--grey';
  }
}
