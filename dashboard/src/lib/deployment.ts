import type { Me } from '../api/types'
import { tk } from '../i18n'

// Authenticated /me alone supplies deployment context. These keys describe a location or an
// instruction whose computer wording cannot be assumed while that answer is missing.
const SERVER_COPY: Record<string, string> = {
  [tk('app.tagline')]: tk('deployment.tagline'),
  [tk('classes.IDENTITY.help')]: tk('deployment.identityHelp'),
  [tk('classes.LLM.help')]: tk('deployment.reviewerHelp'),
  [tk('classes.NER.help')]: tk('deployment.nerHelp'),
  [tk('kinds.outbound_tool')]: tk('deployment.outboundKind'),
  [tk('flags.blocked.help')]: tk('deployment.blockedHelp'),
  [tk('flags.image.help')]: tk('deployment.imageHelp'),
  [tk('flags.outbound.help')]: tk('deployment.outboundHelp'),
  [tk('flags.uninspected.help')]: tk('deployment.uninspectedHelp'),
  [tk('transport.pluginHelp')]: tk('deployment.pluginHelp'),
  [tk('exchange.why.review.happened')]: tk('deployment.reviewHappened'),
  [tk('exchange.why.history.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.plugin.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.uninspected.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.imagesSetting.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.imagesMask.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.localReviewer.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.leak.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.other.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.live.meant')]: tk('deployment.notForwarded'),
  [tk('exchange.why.imagesSetting.todo')]: tk('deployment.imagesTodo'),
  [tk('exchange.why.uninspected.todo')]: tk('deployment.imagesTodo'),
  [tk('exchange.why.other.todo')]: tk('deployment.doctorTodo'),
  [tk('exchange.why.imagesMask.happened')]: tk('deployment.imagesHappened'),
  [tk('exchange.why.imagesMask.todo')]: tk('deployment.imagesTodo'),
  [tk('exchange.why.localReviewer.happened')]: tk('deployment.reviewerHappened'),
  [tk('exchange.why.localReviewer.todo')]: tk('deployment.reviewerTodo'),
  [tk('exchange.why.outbound.happened')]: tk('deployment.outboundHappened'),
  [tk('exchange.why.outbound.todo')]: tk('deployment.outboundTodo'),
  [tk('exchange.why.outbound.ranTitle')]: tk('deployment.outboundRanTitle'),
  [tk('exchange.why.outbound.ranMeant')]: tk('deployment.outboundRanMeant'),
  [tk('exchange.flow.label')]: tk('deployment.flowLabel'),
  [tk('exchange.flow.blocks')]: tk('deployment.flowBlocks'),
  [tk('exchange.panes.original')]: tk('deployment.original'),
  [tk('exchange.response.verifyNote')]: tk('deployment.verifyNote'),
  [tk('vault.description')]: tk('deployment.vault'),
  [tk('history.clear.body')]: tk('deployment.historyClear'),
  [tk('integrations.description')]: tk('deployment.integrations'),
  [tk('reviewer.enabledHelp')]: tk('deployment.reviewerEnabled'),
  [tk('tutorial.elsewhere.body')]: tk('deployment.tutorial'),
  [tk('legend.stoppedHelp')]: tk('deployment.stoppedHelp'),
  [tk('inspector.stopped')]: tk('deployment.stopped'),
  [tk('stats.charts.blockedHelp')]: tk('deployment.statsBlockedHelp'),
  [tk('rules.profile.balanced.help')]: tk('deployment.profileBalanced'),
  [tk('rules.ner.description')]: tk('deployment.nerDescription'),
  [tk('rules.ner.toggleHelp')]: tk('deployment.nerToggleHelp'),
  [tk('reviewer.description')]: tk('deployment.reviewerDescription'),
  [tk('rules.ocr.on')]: tk('deployment.ocrOn'),
  [tk('rules.identity.toggle')]: tk('rules.identity.toggleServer'),
  [tk('rules.identity.detected')]: tk('rules.identity.detectedServer'),
  [tk('rules.review.block')]: tk('deployment.reviewBlock'),
  [tk('rules.review.blockHelp')]: tk('deployment.reviewBlockHelp'),
  [tk('data.images.help')]: tk('deployment.imagesHelp'),
  [tk('integrations.destinations.help')]: tk('deployment.destinationsHelp'),
  [tk('integrations.destinations.inUse')]: tk('deployment.destinationInUse'),
  [tk('integrations.destinations.unused')]: tk('deployment.destinationUnused'),
  [tk('integrations.destinations.empty')]: tk('deployment.destinationsEmpty'),
  [tk('integrations.cli.body')]: tk('deployment.integrationsCli'),
  [tk('live.description')]: tk('deployment.liveDescription'),
  [tk('sessions.newNote')]: tk('deployment.sessionNewNote'),
  [tk('exchange.request.notSentDetail')]: tk('deployment.requestNotSentDetail'),
  [tk('exchange.request.notSentText')]: tk('deployment.requestNotSentText'),
  [tk('rules.review.details.host_nameHelp')]: tk('deployment.hostNameHelp'),
  [tk('rules.recognizers.handEdited')]: tk('deployment.recognizerUnsupported'),
  [tk('rules.ner.inactive')]: tk('deployment.nerInactive'),
  [tk('projects.noFile')]: tk('deployment.projectNoFile'),
  [tk('projects.repo.readOnly')]: tk('deployment.projectRepoReadOnly'),
  [tk('projects.personal.description')]: tk('projects.personal.descriptionServer'),
}

/** What a part that waits for `/me` says meanwhile: that the page loads, and once the answer failed
 * (`unreachable`), to retry the connection. */
export function pendingKey(unreachable: boolean): string {
  return unreachable ? tk('deployment.pending') : tk('common.loading')
}

export function deploymentKey(me: Me | undefined, key: string, unreachable = false): string {
  const server = SERVER_COPY[key]
  if (server === undefined) return key
  if (me === undefined) return key === 'app.tagline' ? tk('deployment.unknownTagline') : pendingKey(unreachable)
  return me.server ? server : key
}
