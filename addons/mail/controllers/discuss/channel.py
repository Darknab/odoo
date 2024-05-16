# Part of Odoo. See LICENSE file for full copyright and licensing details.

from datetime import datetime
from dateutil.relativedelta import relativedelta
from werkzeug.exceptions import NotFound

from odoo import fields, http
from odoo.http import request
from odoo.exceptions import UserError
from odoo.tools import replace_exceptions
from odoo.addons.mail.controllers.webclient import WebclientController
from odoo.addons.mail.models.discuss.mail_guest import add_guest_to_context

class DiscussChannelWebclientController(WebclientController):
    """Override to add discuss channel specific features."""
    def _process_request_for_all(self, store, **kwargs):
        """Override to return channel as member and last messages."""
        super()._process_request_for_all(store, **kwargs)
        if kwargs.get("channels_as_member"):
            channels = request.env["discuss.channel"]._get_channels_as_member()
            # fetch channels data before messages to benefit from prefetching (channel info might
            # prefetch a lot of data that message format could use)
            store.add({"Thread": channels._channel_info()})
            store.add({"Message": channels._get_last_messages()._message_format(for_current_user=True)})


class ChannelController(http.Controller):
    @http.route("/discuss/channel/members", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def discuss_channel_members(self, channel_id, known_member_ids):
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            raise NotFound()
        return channel.load_more_members(known_member_ids)

    @http.route("/discuss/channel/update_avatar", methods=["POST"], type="json")
    def discuss_channel_avatar_update(self, channel_id, data):
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel or not data:
            raise NotFound()
        channel.write({"image_128": data})

    @http.route("/discuss/channel/info", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def discuss_channel_info(self, channel_id):
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            return
        return channel._channel_info()[0]

    @http.route("/discuss/channel/messages", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def discuss_channel_messages(self, channel_id, search_term=None, before=None, after=None, limit=30, around=None):
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            raise NotFound()
        domain = [
            ("res_id", "=", channel_id),
            ("model", "=", "discuss.channel"),
            ("message_type", "!=", "user_notification"),
        ]
        res = request.env["mail.message"]._message_fetch(
            domain, search_term=search_term, before=before, after=after, around=around, limit=limit
        )
        if not request.env.user._is_public() and not around:
            res["messages"].set_message_done()
        return {**res, "messages": res["messages"]._message_format(for_current_user=True)}

    @http.route("/discuss/channel/pinned_messages", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def discuss_channel_pins(self, channel_id):
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            raise NotFound()
        return channel.pinned_message_ids.sorted(key="pinned_at", reverse=True)._message_format(for_current_user=True)

    @http.route("/discuss/channel/mute", methods=["POST"], type="json", auth="user")
    def discuss_channel_mute(self, channel_id, minutes):
        """Mute notifications for the given number of minutes.
        :param minutes: (integer) number of minutes to mute notifications, -1 means mute until the user unmutes
        """
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            raise request.not_found()
        member = channel._find_or_create_member_for_self()
        if not member:
            raise request.not_found()
        if minutes == -1:
            member.mute_until_dt = datetime.max
        elif minutes:
            member.mute_until_dt = fields.Datetime.now() + relativedelta(minutes=minutes)
            request.env.ref("mail.ir_cron_discuss_channel_member_unmute")._trigger(member.mute_until_dt)
        else:
            member.mute_until_dt = False
        channel_data = {
            "id": member.channel_id.id,
            "model": "discuss.channel",
            "mute_until_dt": member.mute_until_dt,
        }
        request.env["bus.bus"]._sendone(member.partner_id, "mail.record/insert", {"Thread": channel_data})

    @http.route("/discuss/channel/update_custom_notifications", methods=["POST"], type="json", auth="user")
    def discuss_channel_update_custom_notifications(self, channel_id, custom_notifications):
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            raise request.not_found()
        member = channel._find_or_create_member_for_self()
        if not member:
            raise request.not_found()
        member.custom_notifications = custom_notifications
        channel_data = {
            "custom_notifications": member.custom_notifications,
            "id": member.channel_id.id,
            "model": "discuss.channel",
        }
        request.env["bus.bus"]._sendone(member.partner_id, "mail.record/insert", {"Thread": channel_data})

    @http.route("/discuss/channel/mark_as_read", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def discuss_channel_mark_as_read(self, channel_id, last_message_id):
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            raise NotFound()
        return channel._mark_as_read(last_message_id)

    @http.route("/discuss/channel/mark_as_unread", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def discuss_channel_mark_as_unread(self, channel_id, message_id):
        with replace_exceptions(ValueError, by=UserError("Invalid `message_id` argument")):
            message_id = int(message_id)
        partner, guest = request.env["res.partner"]._get_current_persona()
        member = request.env["discuss.channel.member"].search([
            ("partner_id", "=", partner.id) if partner else ("guest_id", "=", guest.id),
            ("channel_id", "=", channel_id)
        ])
        if not member:
            raise NotFound()
        return member._set_new_message_separator(message_id, sync=True)

    @http.route("/discuss/channel/notify_typing", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def discuss_channel_notify_typing(self, channel_id, is_typing):
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            raise request.not_found()
        member = channel._find_or_create_member_for_self()
        if not member:
            raise NotFound()
        member._notify_typing(is_typing)

    @http.route("/discuss/channel/attachments", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def load_attachments(self, channel_id, limit=30, before=None):
        """Load attachments of a channel. If before is set, load attachments
        older than the given id.
        :param channel_id: id of the channel
        :param limit: maximum number of attachments to return
        :param before: id of the attachment from which to load older attachments
        """
        channel = request.env["discuss.channel"].search([("id", "=", channel_id)])
        if not channel:
            raise NotFound()
        domain = [
            ["res_id", "=", channel_id],
            ["res_model", "=", "discuss.channel"],
        ]
        if before:
            domain.append(["id", "<", before])
        # sudo: ir.attachment - reading attachments of a channel that the current user can access
        return request.env["ir.attachment"].sudo().search(domain, limit=limit, order="id DESC")._attachment_format()

    @http.route("/discuss/channel/fold", methods=["POST"], type="json", auth="public")
    @add_guest_to_context
    def discuss_channel_fold(self, channel_id, state, state_count):
        member = request.env["discuss.channel.member"].search([("channel_id", "=", channel_id), ("is_self", "=", True)])
        if not member:
            raise NotFound()
        return member._channel_fold(state, state_count)
