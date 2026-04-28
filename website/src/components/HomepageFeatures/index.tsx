import type {ReactNode} from 'react';
import clsx from 'clsx';
import Heading from '@theme/Heading';
import styles from './styles.module.css';

type FeatureItem = {
  title: string;
  Svg: React.ComponentType<React.ComponentProps<'svg'>>;
  description: ReactNode;
};

const FeatureList: FeatureItem[] = [
  {
    title: 'RAM-aware',
    Svg: require('@site/static/img/undraw_docusaurus_mountain.svg').default,
    description: (
      <>
        A single-LLM-slot scheduler keeps only one 7B model resident at a time,
        so a 16GB Mac never thrashes. Status replies cost zero RAM.
      </>
    ),
  },
  {
    title: 'Free-first',
    Svg: require('@site/static/img/undraw_docusaurus_tree.svg').default,
    description: (
      <>
        Free-tier big-AI providers (Gemini, Groq, Ollama Cloud) plan, local
        Ollama executes. Browser fallback covers the rest. $0 budget, real work.
      </>
    ),
  },
  {
    title: 'Drop-in OpenAI',
    Svg: require('@site/static/img/undraw_docusaurus_react.svg').default,
    description: (
      <>
        Exposes an OpenAI-compatible <code>/v1/chat/completions</code> endpoint —
        point opencode, Claude Code, codex, Cursor or Continue at it and go.
      </>
    ),
  },
];

function Feature({title, Svg, description}: FeatureItem) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center">
        <Svg className={styles.featureSvg} role="img" />
      </div>
      <div className="text--center padding-horiz--md">
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures(): ReactNode {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
