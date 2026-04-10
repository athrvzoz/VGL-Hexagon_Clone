import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject, map, catchError, of, tap } from 'rxjs';
import { ContractTask } from '../models/task.model';

@Injectable({
  providedIn: 'root'
})
export class TaskService {
  private http = inject(HttpClient);
  private tasksSubject = new BehaviorSubject<ContractTask[]>([]);
  tasks$ = this.tasksSubject.asObservable();

  constructor() {}

  loadProjectTasks(projectName: string) {
    // Generate filename from project name (replace spaces with underscores)
    const filename = projectName.replace(/ /g, '_') + '.json';
    const filePath = `/api/projects/${filename}`;

    this.http.get<any[]>(filePath).pipe(
      map(data => data.map(item => ({
        id: item.id,
        correspondenceNo: item["Correspondence number"],
        stepName: item["Step Name"],
        submittalDescription: item["Submittal Description"],
        reasonForIssue: item["Reason for Issue"],
        submittalType: item["Submittal Type"],
        targetDate: item["Target Date"],
        creationDate: item["Creation Date"],
        details: item["Details"],
        isClaimed: item["Is Claimed"],
        claimedBy: item["Claimed By"],
        status: item["Status"],
        project: item["Project"],
        author: item["Author"],
        title: item["Title"],
        fromUser: item["From User"],
        toUser: item["To User"],
        revision: "Rev 1",
        vgRevName: item["Correspondence number"],
        issueDate: "3/30/2026 05:35:23",
        vgRevDescription: item["Submittal Description"],
        returnStatus: "NA",
        contractorDocNo: "NA",
        purchaseOrder: "NA",
        originator: "WOR",
        plannedIP: "",
        collector: "WOR",
        revCreationDate: "3/30/2026 05:35:23",
        revCreationUser: "VLG01F/BDabhole",
        lastUpdated: "3/30/2026 05:35:49",
        docStatus: "SUBMIT...",
        versionCreationDate: "3/30/2026 05:35:23"
      }) as ContractTask)),
      catchError(err => {
        console.error(`Failed to load tasks for project: ${projectName}`, err);
        return of([] as ContractTask[]);
      }),
      tap(tasks => this.tasksSubject.next(tasks))
    ).subscribe();
  }

  addTask(task: ContractTask) {
    const currentTasks = this.tasksSubject.value;
    this.tasksSubject.next([...currentTasks, task]);
  }

  claimTask(taskId: string) {
    const currentTasks = this.tasksSubject.value;
    const updatedTasks = currentTasks.map(task =>
      task.id === taskId ? { ...task, isClaimed: true, claimedBy: 'Current User' } : task
    );
    this.tasksSubject.next(updatedTasks);
  }

  updateTask(taskId: string, updates: Partial<ContractTask>) {
    const currentTasks = this.tasksSubject.value;
    const updatedTasks = currentTasks.map(task =>
      task.id === taskId ? { ...task, ...updates } : task
    );
    this.tasksSubject.next(updatedTasks);
  }
}
