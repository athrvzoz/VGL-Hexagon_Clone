import { Injectable, signal, computed, inject } from '@angular/core';
import { ContractTask } from '../models/task.model';
import { TaskService } from './task.service';
import { toSignal } from '@angular/core/rxjs-interop';

@Injectable({
  providedIn: 'root'
})
export class AppStateService {
  private taskService = inject(TaskService);

  // Core State
  activeView = signal<'All' | 'MyTasks' | 'Submitted' | 'Closed' | 'Overdue'>('All');
  selectedProject = signal<string>('C2 PROJECT');
  selectedTask = signal<ContractTask | null>(null);
  viewMode = signal<'DASHBOARD' | 'HISTORY_DETAIL'>('DASHBOARD');

  // UI Toggles
  isSidePanelOpen = signal(false);
  isBottomPanelOpen = signal(false);
  isTransmittalModalOpen = signal(false);

  // Tab States
  sidePanelTab = signal<'TASK' | 'SUBMITTAL' | 'FILES'>('TASK');
  bottomPanelTab = signal<'DETAILS' | 'STRUCTURE' | 'HISTORY' | 'CONTRACT'>('DETAILS');
  transmittalModalTab = signal<'DISTRIBUTION' | 'STRUCTURE' | 'HISTORY'>('DISTRIBUTION');

  // History State
  historyTasks = signal<ContractTask[]>([]);

  // Derived Data
  allTasks = toSignal(this.taskService.tasks$, { initialValue: [] as ContractTask[] });

  filteredTasks = computed(() => {
    const tasks = this.allTasks();
    const project = this.selectedProject();
    const view = this.activeView();

    let filtered = tasks.filter(t => t.project === project);

    if (view === 'MyTasks') {
      return filtered.filter(t => t.isClaimed);
    } else if (view === 'All') {
      return filtered.filter(t => !t.isClaimed); // Show only unclaimed tasks in the inbox
    } else if (view === 'Closed') {
      return filtered.filter(t => t.status === 'Closed');
    } else if (view === 'Submitted') {
      return filtered.filter(t => t.status === 'Submitted');
    } else if (view === 'Overdue') {
      return filtered.filter(t => t.status === 'Overdue');
    }
    return filtered;
  });

  projects = ['C2 PROJECT', 'CX PROJECT', 'ML MARAIS LATERAL', 'PQ EXPANSION PROJECT', 'PD PROJECT', 'VG PROJECT', 'VS SULPHUR'];

  // Stats Derived
  totalCount = computed(() => this.allTasks().filter(t => t.project === this.selectedProject()).length);
  unclaimedCount = computed(() => this.allTasks().filter(t => t.project === this.selectedProject() && !t.isClaimed).length);
  claimedCount = computed(() => this.allTasks().filter(t => t.project === this.selectedProject() && t.isClaimed && t.status !== 'Closed').length);
  submittedCount = computed(() => this.allTasks().filter(t => t.project === this.selectedProject() && t.status === 'Submitted').length);
  closedCount = computed(() => this.allTasks().filter(t => t.project === this.selectedProject() && t.status === 'Closed').length);
  overdueCount = computed(() => this.allTasks().filter(t => t.project === this.selectedProject() && t.status === 'Overdue').length);

  pieChartGradient = computed(() => {
    const total = this.totalCount();
    if (total === 0) return 'conic-gradient(#eee 0 100%)';

    const pClaimed = (this.claimedCount() / total) * 100;
    const pSubmitted = (this.submittedCount() / total) * 100;
    const pClosed = (this.closedCount() / total) * 100;
    const pOverdue = (this.overdueCount() / total) * 100;
    const pUnclaimed = (this.unclaimedCount() / total) * 100;
    
    // Segments: Claimed (#003366), Submitted (#fbbc04), Closed (#4caf50), Overdue (#d93025), Inbox (#ddd)
    return `conic-gradient(
      #003366 0% ${pClaimed}%, 
      #fbbc04 ${pClaimed}% ${pClaimed + pSubmitted}%,
      #4caf50 ${pClaimed + pSubmitted}% ${pClaimed + pSubmitted + pClosed}%, 
      #d93025 ${pClaimed + pSubmitted + pClosed}% ${pClaimed + pSubmitted + pClosed + pOverdue}%,
      #ddd ${pClaimed + pSubmitted + pClosed + pOverdue}% 100%
    )`;
  });

  // Simple 2-segment pie: Claimed vs Unclaimed (All Tasks)
  simplePieData = computed(() => {
    const total = this.totalCount();
    const claimed = this.claimedCount();
    const unclaimed = total - claimed;
    if (total === 0) return { claimedPct: 0, unclaimedPct: 100, total, claimed, unclaimed };
    return {
      claimedPct: (claimed / total) * 100,
      unclaimedPct: (unclaimed / total) * 100,
      total,
      claimed,
      unclaimed
    };
  });

  // CSS conic-gradient for simple 2-color pie (red = claimed, navy = all)
  simplePieGradient = computed(() => {
    const pct = this.simplePieData().claimedPct;
    return `conic-gradient(#c62828 0% ${pct}%, #1a237e ${pct}% 100%)`;
  });

  // Shared Actions
  selectTask(task: ContractTask) {
    this.selectedTask.set(task);
    this.isSidePanelOpen.set(true);
    this.isBottomPanelOpen.set(true);
  }

  finishTransmittal() {
    const task = this.selectedTask();
    if (task) {
      if (confirm('Are you sure you want to finalize this transmittal?')) {
        // 1. Update current task
        const currentUpdates = {
          isClaimed: true,
          claimedBy: 'AD',
          stepName: 'Preparing incoming transmittal'
        };
        this.taskService.updateTask(task.id, currentUpdates);
        this.selectedTask.set({ ...task, ...currentUpdates });

        // 2. Create extra entry
        const newTask: ContractTask = {
          ...task,
          id: `${task.id}-PROC`,
          stepName: 'Transmittal Processed',
          isClaimed: true,
          claimedBy: 'AD'
        };
        this.taskService.addTask(newTask);

        this.isTransmittalModalOpen.set(false);
        alert('Transmittal finalized. Original task updated and follow-up task created.');
      }
    }
  }

  approveTask() {
    const task = this.selectedTask();
    if (task) {
      alert(`Task ${task.id} approved successfully!`);
    }
  }

  declineTask() {
    const task = this.selectedTask();
    if (task) {
      alert(`Task ${task.id} has been declined.`);
    }
  }

  downloadAttachment() {
    const task = this.selectedTask();
    if (task) {
      const pdfUrl = 'https://raw.githubusercontent.com/athrvzoz/LocatorFile/refs/heads/main/CEP210090ER.pdf';
      const link = document.createElement('a');
      link.href = pdfUrl;
      link.download = 'CEP210090ER.pdf';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }
  }

  claimTask() {
    const task = this.selectedTask();
    if (task) {
      this.taskService.updateTask(task.id, { isClaimed: true, claimedBy: 'AD' });
      this.selectedTask.set({ ...task, isClaimed: true, claimedBy: 'AD' });
    }
  }

  selectProject(project: string) {
    this.selectedProject.set(project);
    this.taskService.loadProjectTasks(project);
  }

  exportData() {
    const task = this.selectedTask();
    if (task && task.project === 'C2 PROJECT') {
      const csvUrl = 'https://raw.githubusercontent.com/athrvzoz/LocatorFile/refs/heads/main/Sample%20Loadsheet(C2)%20(2).csv';
      fetch(csvUrl)
        .then(response => response.blob())
        .then(blob => {
          const blobUrl = window.URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = blobUrl;
          link.download = 'Sample_Loadsheet(C2).csv';
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          window.URL.revokeObjectURL(blobUrl);
        })
        .catch(error => {
          console.error('Download failed:', error);
        });
    } else if (task) {
      alert('Export data is only available for C2 PROJECT tasks.');
    }
  }

  extractDiscipline(correspondenceNo?: string): string {
    if (!correspondenceNo) return 'DMG';
    const parts = correspondenceNo.split('-');
    return parts.length > 1 ? parts[1] : 'DMG';
  }

  openHistoryModal() {
    const task = this.selectedTask();
    if (task) {
      const currentRev = { ...task, revision: '1', docStatus: 'SUBMIT...', revCreationUser: 'VLG01F/BDabhole' };
      const previousRev: ContractTask = {
        ...task,
        id: `${task.id}-R0`,
        revision: '0',
        issueDate: '2/20/2026 04:34:13',
        creationDate: '2/20/2026 04:34:13',
        revCreationDate: '2/20/2026 04:34:13',
        revCreationUser: 'VGL01F/VShelar',
        lastUpdated: '3/30/2026 05:35:23',
        docStatus: 'Current',
        status: 'Closed'
      };
      this.historyTasks.set([currentRev, previousRev]);
      this.viewMode.set('HISTORY_DETAIL');
      this.isSidePanelOpen.set(false);
      this.isBottomPanelOpen.set(false);
    }
  }
}
