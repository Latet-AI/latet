import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { ExchangeImage, Slot } from '../../api/types'
import { tk } from '../../i18n'
import { Icon } from '../Icon'

const STATE: Record<ExchangeImage['state'], { label: string; help: string }> = {
  masked: { label: tk('exchange.images.masked'), help: tk('exchange.images.maskedHelp') },
  clean: { label: tk('exchange.images.clean'), help: tk('exchange.images.cleanHelp') },
  withheld: { label: tk('exchange.images.withheld'), help: tk('exchange.images.withheldHelp') },
}

/** The request's images as they were sent, each with the block of the text read in it. */
export function ExchangeImages({ images, slots }: { images: ExchangeImage[]; slots: Slot[] }) {
  const { t } = useTranslation()
  if (images.length === 0) return null
  return (
    <div className="mb-4">
      <h3 className="mb-2 flex items-center gap-2 text-ui font-semibold text-ink">
        <Icon name="image" size={15} />
        {t('exchange.images.title', { count: images.length })}
      </h3>
      <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {images.map((image, n) => (
          <ImageCard key={`${image.path}:${n}`} image={image} block={slots.findIndex((s) => s.block === image.block)} />
        ))}
      </ul>
    </div>
  )
}

function ImageCard({ image, block }: { image: ExchangeImage; block: number }) {
  const { t } = useTranslation()
  const [large, setLarge] = useState(false)
  const state = STATE[image.state]
  const shown = image.id !== null && image.kept !== false
  return (
    <li className="panel overflow-hidden">
      {shown ? (
        <button
          type="button"
          onClick={() => setLarge((v) => !v)}
          aria-expanded={large}
          className="block w-full bg-well text-left"
          title={large ? t('exchange.images.smaller') : t('exchange.images.larger')}
        >
          <img
            src={api.imageUrl(image.id as string)}
            alt={t('exchange.images.alt', { state: t(state.label) })}
            className={`mx-auto block w-full object-contain ${large ? '' : 'max-h-56'}`}
          />
        </button>
      ) : (
        <div className="flex h-28 items-center justify-center bg-well px-4 text-center text-meta text-ink-3">
          {image.state === 'withheld' ? t('exchange.images.notSent') : t('exchange.images.notKept')}
        </div>
      )}
      <div className="space-y-1 px-3 py-2">
        <p className="text-ui font-semibold text-ink">{t(state.label)}</p>
        <p className="text-meta text-ink-2">{t(state.help)}</p>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-meta">
          <code className="mono min-w-0 truncate text-ink-3" title={image.path}>
            {image.path}
          </code>
          {block >= 0 && (
            <a href={`#block-${block}`} className="link">
              {t('exchange.images.text', { number: block + 1 })}
            </a>
          )}
        </p>
      </div>
    </li>
  )
}
