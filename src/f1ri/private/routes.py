"""Owner-bound workspace endpoints. Browser-supplied ownership is excluded by schema."""

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import UUID4

from f1ri.api.research import ResearchError, verify_workspace
from f1ri.api.workspace import WorkspaceFile
from f1ri.decision_board import canonical
from f1ri.private.auth import owner


def workspace_routes(app, root, store, config):
    api = APIRouter(prefix="/api/v1/workspaces")

    @api.get("", response_model=list[WorkspaceFile])
    def listing(request: Request):
        return store.workspaces(owner(request))

    @api.post("", response_model=WorkspaceFile, status_code=201)
    def create(document: WorkspaceFile, request: Request):
        verify_workspace(root, document)
        return store.save(owner(request), document, config.workspace_limit)

    @api.get("/{identity}", response_model=WorkspaceFile)
    def read(identity: UUID4, request: Request):
        return store.workspace(owner(request), str(identity))

    @api.put("/{identity}", response_model=WorkspaceFile)
    def update(identity: UUID4, document: WorkspaceFile, request: Request):
        account = owner(request)
        store.workspace(account, str(identity))
        if document.id != identity:
            raise ResearchError("WORKSPACE_ID_MISMATCH", "The document ID must follow the selected workspace.", 409)
        verify_workspace(root, document)
        return store.save(account, document, config.workspace_limit, update=True)

    @api.delete("/{identity}", status_code=204)
    def remove(identity: UUID4, request: Request):
        store.delete(owner(request), str(identity))
        return Response(status_code=204)

    @api.post("/{identity}/duplicate", response_model=WorkspaceFile, status_code=201)
    def duplicate(identity: UUID4, request: Request):
        account = owner(request)
        original = store.workspace(account, str(identity))
        copy = original.model_copy(update={"id": uuid4(), "title": original.title[:73]+" (copy)"})
        return store.save(account, copy, config.workspace_limit)

    @api.get("/{identity}/export")
    def export(identity: UUID4, request: Request):
        account = owner(request)
        document = store.workspace(account, str(identity))
        store.limit(account, "export", config.rate_exports)
        return Response(canonical(document.model_dump(mode="json")), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="f1ri-workspace-{identity}.json"'})

    app.include_router(api)
