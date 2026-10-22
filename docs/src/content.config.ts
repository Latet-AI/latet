import { defineCollection } from 'astro:content';
import { z } from 'astro/zod';
import { docsLoader, i18nLoader } from '@astrojs/starlight/loaders';
import { docsSchema, i18nSchema } from '@astrojs/starlight/schema';

export const collections = {
	docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
	// UI strings we add ourselves, one file per locale in src/content/i18n/.
	// Starlight's own UI strings are already translated and can be overridden here too.
	i18n: defineCollection({
		loader: i18nLoader(),
		schema: i18nSchema({
			extend: z.object({
				// The footer's legal line (src/components/Footer.astro).
				'latet.footer.vat': z.string().optional(),
				'latet.footer.privacy': z.string().optional(),
				'latet.footer.legal': z.string().optional(),
				// The product video's description for screen readers (src/components/ProductVideoPlayer.astro).
				'latet.video.label': z.string().optional(),
				// The link that stands for the video in the copy Latet serves itself (src/components/ProductVideo.astro).
				'latet.video.watch': z.string().optional(),
			}),
		}),
	}),
};
