module Services
  module Freespins
    class ActivateByUser
      include A8rApiV2::Import["core.logger", "services.freespins.calculator"]

      def call(user_id)
        logger.info("activating")
        calculator.run(user_id)
      end
    end
  end
end
