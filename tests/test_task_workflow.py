import unittest
from datetime import datetime, timezone
from task_workflow import WorkflowError, create_task, assign, accept, decline, complete

class TaskWorkflowTests(unittest.TestCase):
    def test_pool_claim_complete_and_notify(self):
        task, events = create_task("t1", "Restock display", "owner")
        self.assertEqual((task.status, events), ("available", ()))
        task = accept(task, "employee")
        self.assertEqual(task.assignee_id, "employee")
        finished, notifications = complete(task, "employee", "Filled shelves",
            datetime(2026, 10, 23, tzinfo=timezone.utc))
        self.assertEqual((finished.status, finished.completion_summary), ("completed", "Filled shelves"))
        self.assertEqual(finished.archived_at, finished.completed_at)
        self.assertEqual((notifications[0].type, notifications[0].recipient_id), ("task_completed", "owner"))
        with self.assertRaises(WorkflowError): complete(finished, "employee")

    def test_direct_accept_and_decline(self):
        task, notifications = create_task("t2", "Clean windows", "owner", assignee_id="employee")
        self.assertEqual(notifications[0].recipient_id, "employee")
        with self.assertRaises(WorkflowError): accept(task, "someone_else")
        open_task, messages = decline(task, "employee")
        self.assertEqual((open_task.status, open_task.assignee_id), ("available", None))
        self.assertEqual(messages[0].recipient_id, "owner")
        assigned, _ = assign(open_task, "owner", "other")
        working = accept(assigned, "other")
        self.assertEqual(working.status, "in_progress")

    def test_private_tasks_do_not_leak(self):
        with self.assertRaises(WorkflowError):
            create_task("t3", "Private", "owner", scope="personal", assignee_id="employee")
        private, _ = create_task("t4", "Private", "owner", scope="personal")
        with self.assertRaises(WorkflowError): accept(private, "employee")
        with self.assertRaises(WorkflowError): assign(private, "owner", "employee")

    def test_unauthorized_completion_denied(self):
        task, _ = create_task("t5", "Check shelves", "owner", assignee_id="employee")
        with self.assertRaises(WorkflowError): complete(task, "other")
        with self.assertRaises(WorkflowError): complete(task, "employee")
        accepted = accept(task, "employee")
        with self.assertRaises(WorkflowError): complete(accepted, "other")

if __name__ == "__main__":
    unittest.main()
