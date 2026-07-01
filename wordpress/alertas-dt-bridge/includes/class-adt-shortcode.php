<?php
defined( 'ABSPATH' ) || exit;

class ADT_Shortcode {

    public static function register(): void {
        add_shortcode( 'alertas_dt_form', [ __CLASS__, 'render' ] );
        add_action( 'wp_enqueue_scripts', [ __CLASS__, 'enqueue_assets' ] );
        add_action( 'wp_ajax_adt_subscribe',        [ __CLASS__, 'handle_ajax' ] );
        add_action( 'wp_ajax_nopriv_adt_subscribe', [ __CLASS__, 'handle_ajax' ] );
    }

    public static function enqueue_assets(): void {
        wp_enqueue_style(
            'alertas-dt-form',
            ADT_PLUGIN_URL . 'assets/css/public-form.css',
            [],
            ADT_VERSION
        );
        wp_enqueue_script(
            'alertas-dt-form',
            ADT_PLUGIN_URL . 'assets/js/public-form.js',
            [],
            ADT_VERSION,
            true
        );
        wp_localize_script( 'alertas-dt-form', 'adtConfig', [
            'ajaxUrl' => admin_url( 'admin-ajax.php' ),
            'nonce'   => wp_create_nonce( 'adt_subscribe' ),
            'msgs'    => [
                'success_new'     => 'Listo, quedaste inscrito en Alertas DT.',
                'success_updated' => 'Tu suscripción ya existía y fue actualizada correctamente.',
                'error_email'     => 'Ingresa un correo electrónico válido.',
                'error_consent'   => 'Debes aceptar recibir alertas para continuar.',
                'error_generic'   => 'No pudimos registrar tu suscripción. Intenta nuevamente en unos minutos.',
            ],
        ] );
    }

    public static function render( array $atts ): string {
        $atts = shortcode_atts( [
            'source_page' => get_post_field( 'post_name', get_the_ID() ) ?: 'wordpress',
        ], $atts, 'alertas_dt_form' );

        $source_page = sanitize_text_field( $atts['source_page'] );
        $nonce       = wp_create_nonce( 'adt_subscribe' );
        $source_url  = esc_url( ( is_ssl() ? 'https' : 'http' ) . '://' . $_SERVER['HTTP_HOST'] . $_SERVER['REQUEST_URI'] );

        ob_start();
        ?>
        <form class="eg-card eg-form alertas-dt-form" data-eg-theme="light"
              method="post" action="" novalidate
              data-adt-form="1">

            <input type="hidden" name="action"      value="adt_subscribe">
            <input type="hidden" name="nonce"       value="<?php echo esc_attr( $nonce ); ?>">
            <input type="hidden" name="source_page" value="<?php echo esc_attr( $source_page ); ?>">
            <input type="hidden" name="source_url"  value="<?php echo esc_attr( $source_url ); ?>">

            <!-- Honeypot: debe quedar vacío -->
            <div class="adt-hp" aria-hidden="true" style="display:none!important;">
                <input type="text" name="adt_website" tabindex="-1" autocomplete="off">
            </div>

            <p class="eg-eyebrow">Suscripción</p>
            <h2 class="eg-form__title">Activa tus alertas</h2>

            <div class="alertas-dt-message" role="alert" aria-live="polite" style="display:none;"></div>

            <div class="eg-field">
                <label class="eg-label" for="adt-name">Nombre (opcional)</label>
                <input class="eg-input" id="adt-name" name="subscriber_name" type="text"
                       placeholder="Tu nombre" autocomplete="name">
            </div>

            <div class="eg-field">
                <label class="eg-label" for="adt-email">Correo electrónico</label>
                <input class="eg-input" id="adt-email" name="email" type="email"
                       required placeholder="nombre@empresa.cl" autocomplete="email">
            </div>

            <div class="eg-field">
                <label class="eg-label" for="adt-phone">Teléfono (opcional)</label>
                <input class="eg-input" id="adt-phone" name="phone" type="tel"
                       placeholder="+56 9 1234 5678" autocomplete="tel">
            </div>

            <label class="eg-check eg-check--consent">
                <input type="checkbox" name="consent" value="1" required>
                <span>Acepto recibir alertas informativas por email sobre nuevas publicaciones de la Dirección del Trabajo.</span>
            </label>

            <label class="eg-check eg-check--whatsapp" id="adt-whatsapp-check" style="display:none;">
                <input type="checkbox" name="whatsapp_consent" value="1" id="adt-whatsapp-consent">
                <span>También acepto recibir alertas por WhatsApp al número indicado.</span>
            </label>

            <button class="eg-btn eg-btn--primary eg-btn--block" type="submit">
                Suscribirme a las alertas
            </button>

            <p class="eg-fineprint">
                Podrás solicitar la baja cuando quieras. Los resúmenes son informativos y no reemplazan la revisión del documento oficial.
            </p>
        </form>

        <script>
        (function(){
            var phone = document.getElementById('adt-phone');
            var wrap  = document.getElementById('adt-whatsapp-check');
            var cb    = document.getElementById('adt-whatsapp-consent');
            if (!phone || !wrap) return;
            phone.addEventListener('input', function(){
                var show = phone.value.trim().length > 0;
                wrap.style.display = show ? '' : 'none';
                if (!show) cb.checked = false;
            });
        })();
        </script>

        <noscript>
            <form class="eg-card eg-form alertas-dt-form" method="post"
                  action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" novalidate>
                <input type="hidden" name="action"      value="adt_subscribe_nojs">
                <input type="hidden" name="_wpnonce"    value="<?php echo esc_attr( wp_create_nonce( 'adt_subscribe_nojs' ) ); ?>">
                <input type="hidden" name="source_page" value="<?php echo esc_attr( $source_page ); ?>">
                <input type="hidden" name="source_url"  value="<?php echo esc_attr( $source_url ); ?>">
                <div class="eg-field">
                    <label class="eg-label" for="adt-email-njs">Correo electrónico</label>
                    <input class="eg-input" id="adt-email-njs" name="email" type="email"
                           required placeholder="nombre@empresa.cl">
                </div>
                <label class="eg-check eg-check--consent">
                    <input type="checkbox" name="consent" value="1" required>
                    <span>Acepto recibir alertas informativas por email sobre nuevas publicaciones de la Dirección del Trabajo.</span>
                </label>
                <button class="eg-btn eg-btn--primary eg-btn--block" type="submit">Suscribirme a las alertas</button>
            </form>
        </noscript>
        <?php
        return ob_get_clean();
    }

    public static function handle_ajax(): void {
        $nonce = isset( $_POST['nonce'] ) ? sanitize_text_field( wp_unslash( $_POST['nonce'] ) ) : '';
        if ( ! wp_verify_nonce( $nonce, 'adt_subscribe' ) ) {
            wp_send_json_error( [ 'message' => 'No pudimos registrar tu suscripción. Intenta nuevamente en unos minutos.' ], 403 );
        }

        // Honeypot check
        if ( ! empty( $_POST['adt_website'] ) ) {
            wp_send_json_success( [ 'message' => 'Listo, quedaste inscrito en Alertas DT.' ] );
        }

        $email   = isset( $_POST['email'] )   ? sanitize_email( wp_unslash( $_POST['email'] ) )   : '';
        $consent = ! empty( $_POST['consent'] );

        try {
            $result = ADT_Database::upsert( [
                'email'            => $email,
                'consent'          => $consent,
                'source_page'      => isset( $_POST['source_page'] )     ? sanitize_text_field( wp_unslash( $_POST['source_page'] ) )     : null,
                'source_url'       => isset( $_POST['source_url'] )      ? esc_url_raw( wp_unslash( $_POST['source_url'] ) )              : null,
                'ip'               => $_SERVER['REMOTE_ADDR'] ?? '',
                'user_agent'       => $_SERVER['HTTP_USER_AGENT'] ?? '',
                'subscriber_name'  => isset( $_POST['subscriber_name'] ) ? sanitize_text_field( wp_unslash( $_POST['subscriber_name'] ) ) : null,
                'phone'            => isset( $_POST['phone'] )           ? sanitize_text_field( wp_unslash( $_POST['phone'] ) )           : null,
                'whatsapp_consent' => ! empty( $_POST['whatsapp_consent'] ),
            ] );
            $msg = $result['created']
                ? 'Listo, quedaste inscrito en Alertas DT.'
                : 'Tu suscripción ya existía y fue actualizada correctamente.';
            wp_send_json_success( [ 'message' => $msg, 'created' => $result['created'] ] );
        } catch ( InvalidArgumentException $e ) {
            wp_send_json_error( [ 'message' => $e->getMessage() ], 422 );
        } catch ( Throwable $e ) {
            wp_send_json_error( [ 'message' => 'No pudimos registrar tu suscripción. Intenta nuevamente en unos minutos.' ], 500 );
        }
    }
}

// Fallback no-JS
add_action( 'admin_post_nopriv_adt_subscribe_nojs', function () {
    if ( ! wp_verify_nonce( $_POST['_wpnonce'] ?? '', 'adt_subscribe_nojs' ) ) {
        wp_die( 'Solicitud inválida.' );
    }
    try {
        ADT_Database::upsert( [
            'email'      => sanitize_email( wp_unslash( $_POST['email'] ?? '' ) ),
            'consent'    => ! empty( $_POST['consent'] ),
            'source_page' => sanitize_text_field( wp_unslash( $_POST['source_page'] ?? '' ) ),
            'source_url'  => esc_url_raw( wp_unslash( $_POST['source_url'] ?? '' ) ),
            'ip'         => $_SERVER['REMOTE_ADDR'] ?? '',
            'user_agent' => $_SERVER['HTTP_USER_AGENT'] ?? '',
        ] );
        wp_safe_redirect( add_query_arg( 'adt', 'ok', wp_get_referer() ?: home_url() ) );
    } catch ( Throwable $e ) {
        wp_safe_redirect( add_query_arg( 'adt', 'error', wp_get_referer() ?: home_url() ) );
    }
    exit;
} );
