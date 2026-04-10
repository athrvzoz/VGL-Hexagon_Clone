import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AppStateService } from '../../services/app-state.service';

@Component({
  selector: 'app-transmittal-modal',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './transmittal-modal.component.html',
  styleUrl: './transmittal-modal.component.css'
})
export class TransmittalModalComponent {
  state = inject(AppStateService);

  closeModal() {
    this.state.isTransmittalModalOpen.set(false);
  }
}
