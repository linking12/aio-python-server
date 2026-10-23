import io
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field
from starlette.responses import StreamingResponse

from app.core.service_container import services
from app.models.browser import ActionResponse, AnyAction, BrowserInfoResult
from app.schemas.browser import BrowserConfigRequest
from app.schemas.response import Response
from app.services.browser import BrowserService


logger = logging.getLogger(__name__)


router = APIRouter()


@router.get(
    '/info',
    response_model=Response[BrowserInfoResult],
    openapi_extra={
        'x-fern-sdk-group-name': 'browser',
        'x-fern-sdk-method-name': 'get_info',
    },
)
async def get_browser_info(request: Request):
    """
    Get information about browser, like cdp url, viewport size, etc.
    """
    browser_service: BrowserService = services.require('browser_service')
    result = await browser_service.get_browser_info(request=request)

    return Response(
        success=True,
        message='Browser info retrieved',
        data=result,
    )


@router.get(
    '/screenshot',
    openapi_extra={
        'x-fern-sdk-group-name': 'browser',
        'x-fern-sdk-method-name': 'screenshot',
    },
    response_class=StreamingResponse,
    responses={
        200: {
            'description': 'Screenshot image',
            'content': {'image/png': {}, 'image/jpeg': {}},
            'headers': {
                'x-screen-width': {
                    'description': 'Screen width',
                    'schema': {'type': 'string'},
                },
                'x-screen-height': {
                    'description': 'Screen height',
                    'schema': {'type': 'string'},
                },
                'x-image-width': {
                    'description': 'Image width',
                    'schema': {'type': 'string'},
                },
                'x-image-height': {
                    'description': 'Image height',
                    'schema': {'type': 'string'},
                },
            },
        }
    },
)
async def take_screenshot(
    format: Literal['png', 'jpg', 'jpeg'] = Query('png'),
    quality: int | None = Query(None, ge=0, le=100),
):
    """Take a screenshot of the current display.

    Returns:
        StreamingResponse: PNG or JPEG image data with proper headers including display and screenshot dimensions
    """

    browser_service: BrowserService = services.require('browser_service')

    img, result = await browser_service.task_screenshot()

    buf = io.BytesIO()
    image_format = 'jpeg' if format in {'jpg', 'jpeg'} else 'png'
    if image_format == 'jpeg':
        save_kwargs = {'optimize': True}
        if quality is not None:
            save_kwargs['quality'] = quality
        else:
            save_kwargs['quality'] = 85
        img.convert('RGB').save(buf, 'JPEG', **save_kwargs)
    else:
        img.save(buf, 'PNG')
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type='image/jpeg' if image_format == 'jpeg' else 'image/png',
        headers={
            'x-screen-width': str(result.display_width),
            'x-screen-height': str(result.display_height),
            'x-image-width': str(result.screenshot_width),
            'x-image-height': str(result.screenshot_height),
        },
    )


@router.post(
    '/actions',
    openapi_extra={
        'x-fern-sdk-group-name': 'browser',
        'x-fern-sdk-method-name': 'execute_action',
    },
    response_model=ActionResponse,
)
async def execute_action(
    action: Annotated[AnyAction, Field(discriminator='action_type')],
) -> ActionResponse:
    """Execute a validated action on the current display."""
    browser_service: BrowserService = services.require('browser_service')
    action_response = await browser_service.execute_action(action)

    return action_response


@router.post(
    '/config',
    openapi_extra={
        'x-fern-sdk-group-name': 'browser',
        'x-fern-sdk-method-name': 'set_config',
    },
    response_model=Response,
)
async def set_config(request: BrowserConfigRequest) -> ActionResponse:
    """Execute a validated action on the current display."""
    browser_service: BrowserService = services.require('browser_service')

    if request.resolution:
        message = browser_service.set_resolution(
            width=request.resolution.width, height=request.resolution.height
        )
        return Response(success=True, message=message, data=None)

    return Response(
        success=False, message='No configuration changes provided', data=None
    )
